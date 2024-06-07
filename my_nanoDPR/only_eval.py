# %%
## built-in
import time,random,queue,sys
import math,logging,json,random,os,psutil
import types
os.environ["TOKENIZERS_PARALLELISM"]='true'
os.environ["WANDB_IGNORE_GLOBS"]='*.bin' ## not upload ckpt to wandb cloud
os.environ["CUDA_LAUNCH_BLOCKING"]="1" ## for debugging
import gc

## third-party
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import DistributedDataParallelKwargs
import transformers
transformers.logging.set_verbosity_error()
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributed as dist
from tqdm import tqdm

## own
from utils import (
    ensure_directory_exists_for_file,
    get_yaml_file,
    set_seed,
    get_linear_scheduler,
    normalize_query,
    retrieve_top_k_docid,
    load_lm_model_and_tokenizer,
    get_lm_prob,
    get_t5_lm_prob,
    lm_gen_and_check,
    load_query_encoder_and_tokenizer,
    make_prompt,
)

debug = True # set log mode to debug, and stop wandb logging
max_ret_token_len = 0
max_lm_token_len = 0

logging.basicConfig(level=logging.DEBUG if debug else logging.INFO)
logger = get_logger(__name__)

def parse_args():
    # # When using ipynb
    # config_file = 'config/eval_dpr_nq.yaml'
    # yaml_config = get_yaml_file(config_file)
    # args_dict = {}
    # args_dict['config_file'] = config_file

    # When using cmd line
    import argparse
    parser = argparse.ArgumentParser()
    ## adding args here for more control from CLI is possible
    parser.add_argument("--config_file",default='config/eval_dpr_nq.yaml')
    args = parser.parse_args()
    args_dict = {k:v for k,v in vars(args).items() if v is not None}
    yaml_config = get_yaml_file(args.config_file)

    yaml_config.update(args_dict)
    args = types.SimpleNamespace(**yaml_config) # access in attribute style
    return args

# def calculate_dpr_loss(matching_score,labels):
#     return F.nll_loss(input=F.log_softmax(matching_score,dim=1),target=labels)

def calculate_KL_div_loss(
    input_logits, # size [n_question,n_comb]
    target_logits, # size [n_question,n_comb]
    temperature, # [ret_temperature,lm_temperature]
):
    """
    Calculate KL divergence loss between input and target logits
    Note: input_logits and target_logits are logits, not distributions
    """
    global logger
    # logger.debug(f"input_logits: {F.softmax(input_logits / temperature, dim=1)}")
    # logger.debug(f"target_logits: {F.softmax(target_logits / temperature, dim=1)}")
    kl_loss = nn.KLDivLoss(reduction="batchmean")
    loss = kl_loss(
        F.log_softmax(input_logits / temperature[0], dim=1), # input should be a distribution in the log space
        F.softmax(target_logits / temperature[1], dim=1),
    )
    return loss

def calculate_cross_entropy_loss(
    input_logits, # [n_question,n_comb]
    target_logits, # [n_question,n_comb]
    temperature, # [ret_temperature,lm_temperature]
):
    """
    Calculate cross entropy loss between input and target logits
    Take the argmax of target_logits as the label
    """
    global logger
    # logger.debug(f"input_logits: {F.softmax(input_logits / temperature, dim=1)}")
    # logger.debug(f"target_logits: {F.softmax(target_logits / temperature, dim=1)}")
    ce_loss = nn.CrossEntropyLoss() # reduction is mean by default
    input_logits = input_logits / temperature[0]
    loss = ce_loss(
        input=input_logits, # input is expected to contain the unnormalized logits for each class
        target=torch.argmax(target_logits, dim=1),
    )
    return loss 

def calculate_nll_loss(
    doc_scores, # [n_question,n_comb]
    seq_probs, # [n_question,n_comb]
):
    """
    Following RAG paper, calculate negative log likelihood loss
    prob_y_given_x = doc similarity score * answer probability
    NLL = -log(prob_y_given_x)
    """
    # version 1.
    # ref: https://github.com/huggingface/transformers/blob/v4.41.2/src/transformers/models/rag/modeling_rag.py#L1057
    doc_logprobs = nn.functional.log_softmax(doc_scores, dim=1)
    seq_logprobs = seq_probs.log()
    # special handling: if any row has -inf, replace it with smallest float
    # if this isn't handled, loss will go to inf -> exploding gradient
    seq_logprobs[seq_logprobs == float("-inf")] = torch.finfo(seq_logprobs.dtype).min
    nll_loss = -(doc_logprobs + seq_logprobs).logsumexp(dim=1).mean()

    # version 2. (Turns out to be the same as version 1.)
    # doc_probs = nn.functional.softmax(doc_scores, dim=1)
    # seq_logprobs[seq_logprobs == 0] = seq_logprobs[seq_logprobs != 0].min() # for numerical stability
    # nll_loss = -(doc_probs * seq_probs).log().sum(dim=1).mean()

    if nll_loss.isnan():
        global logger
        logger.warning("nll_loss is nan!")
        logger.info(f"doc_logprobs: {doc_logprobs}")
        logger.info(f"seq_logprobs: {seq_logprobs}")
    return nll_loss

class QADataset(torch.utils.data.Dataset):
    def __init__(self, qa_pairs, all_corpus, all_doc_embeddings):
        self.qa_pairs = qa_pairs
        self.all_corpus = all_corpus
        self.all_doc_embeddings = all_doc_embeddings
        
    def __len__(self):
        return len(self.qa_pairs)

    def __getitem__(self, idx):
        data = [self.qa_pairs[idx]]  # each item is (query, all_doc, answer, last_doc_embedding, qid)
        corpus = self.all_corpus[idx]
        doc_embeddings = self.all_doc_embeddings[idx]
        return {"data": data, "corpus": corpus, "doc_embeddings": doc_embeddings}
    
    def collate_fn(self, samples):
        """
        samples: List[Dict]
        """
        return samples

# this is like getitem, moved outside Dataset because we're using GPU here, and using GPU inside Dataset is not recommended
# def inloop_getitem
def inloop_extend_item(data, corpus, doc_embeddings, ret_tokenizer, query_encoder, args):
    """
    Extend each item in data by retrieving top k documents for each round
    into 1 + k + k^2 + ... + k^max_round items
    data: List[tuple], each tuple is (query, all_doc, answer, last_doc_embedding)
    """
    global logger
    # logger.debug("In inloop_getitem...")
    embedding_device = data[0][3].device

    # Initialize pointers
    cur_visited = 0
    this_round_should_visited = 0
    next_round_should_visited = len(data)

    for i_rnd in range(args.max_round):
        # logger.debug(f"Round {i_rnd} has {next_round_should_visited} data to go thru...")
        # Update pointers
        this_round_should_visited = next_round_should_visited
        next_round_should_visited = 0
        cur_visited = 0
        # Process data from current round
        while cur_visited < this_round_should_visited:
            # Get current data
            query, doc_list, answer, _ = data[cur_visited]
            cur_visited += 1

            # Retrieve top k documents
            with torch.no_grad():
                doc_ids = retrieve_top_k_docid(
                    doc_list[-1] + " " + query, 
                    doc_embeddings, 
                    ret_tokenizer, 
                    query_encoder, 
                    args.k,
                    ids_to_exclude=[],
                )
            # Append new data
            for docid in doc_ids:
                new_doc_list = doc_list + [corpus[docid]]
                data.append((query, new_doc_list, answer, doc_embeddings[docid].to(embedding_device)))

                # Increment next_pointer
                next_round_should_visited += 1

    # debug: temporarily remove empty document
    if not args.empty_doc:
        num_data_before_remove = len(data)
        data = [x for x in data if x[1] != [""]]
        num_data_after_remove = len(data)
        assert num_data_before_remove == num_data_after_remove + 1, f"num_data_before_remove ({num_data_before_remove}) != num_data_after_remove + 1 ({num_data_after_remove + 1})"

    return data  # List of tuples

# %%
# this is like collate_fn, moved outside Dataset because getitem and collate_fn should be in the same scope
def inloop_collate_fn(samples, ret_tokenizer, lm_tokenizer, lm_name, args, mode="train"):
    """
    Construct a batch.
    samples: List[List[tuple]]
    """
    global logger
    # TODO add feature: 不同文章數量的分開 decode
    # flatten the samples into a list of tuples
    # logger.debug(f"Original batch size: {len(samples)}")
    num_orig_question = len(samples)
    samples = [item for sublist in samples for item in sublist]
    # logger.debug(f"Real batch size: {len(samples)}")
    
    # each item is (query, all_doc, answer, last_doc_embedding)
    query_inputs = ret_tokenizer([x[0] for x in samples], max_length=256, padding=True, truncation=True, return_tensors='pt')
    # collect doc_inputs from doc_embeddings
    doc_embeddings = torch.stack([x[3] for x in samples], dim=0)
    
    prompt = [make_prompt(
        question=x[0], documents=x[1], lm_name=lm_name, 
        num_exemplars=args.num_exemplars, dataset=args.dataset_name) for x in samples]
    answer_to_encode = [x[2][0] for x in samples] # pick the first answer for each question, as eval set may have multiple answers
    # debug
    # print(f"Real answer: {answer}")

    if "t5" in lm_name:
        # separate input_ids (send into encoder) and labels (send into decoder)
        # regarding max_length: https://huggingface.co/google/flan-t5-xxl/discussions/41
        # regarding max_length: https://github.com/google-research/FLAN/issues/36
        input_ids = lm_tokenizer(prompt, return_tensors="pt", padding=True, truncation=True, max_length=2048).input_ids
        labels = lm_tokenizer(answer_to_encode, return_tensors="pt", padding=True, truncation=True, max_length=512).input_ids
        prompt_ans_lm_inputs = {"input_ids": input_ids, "labels": labels}
    else:
        if "Llama-3" in lm_name:
            max_length = 8000
            lm_tokenizer.pad_token_id = lm_tokenizer.eos_token_id
        if "Llama-2" in lm_name:
            max_length = 4096
        elif "llama-" in lm_name: # llama 1
            max_length = 2048
        elif "gpt2" in lm_name:
            max_length = 1024
        else:
            max_length = 256
        
        # add_special_tokens=False is really important as it affects probability A LOT!
        # those bos, eos are already added in make_prompt for llama3
        prompt_ans_lm_inputs = lm_tokenizer(
            prompt, answer_to_encode, max_length=max_length, padding=True, truncation=True, 
            return_tensors='pt', return_token_type_ids=True,
            add_special_tokens=False if "Llama-3" in lm_name else True,
        ) # dict of keys: input_ids, attention_mask, token_type_ids. Each of shape [num_orig_question * n_comb, n_dim]
    
    # update max token length
    global max_ret_token_len, max_lm_token_len
    max_ret_token_len = max(max_ret_token_len, query_inputs["input_ids"].shape[1])
    max_lm_token_len = max(max_lm_token_len, prompt_ans_lm_inputs["input_ids"].shape[1])

    res_dict = {
        "query_inputs": query_inputs, # dict
        "doc_embeddings": doc_embeddings, # tensor, [bs,n_dim]
        "prompt_ans_lm_inputs": prompt_ans_lm_inputs, # dict
    }
    if mode == "eval":
        n_comb = prompt_ans_lm_inputs["input_ids"].shape[0] // num_orig_question
        res_dict["full_answers"] = [x[2] for i, x in enumerate(samples) if i % n_comb == 0] # list of list of str; len = num_orig_question
        assert len(res_dict["full_answers"]) == num_orig_question, f"len(res_dict['full_answers']) ({len(res_dict['full_answers'])}) != num_orig_question ({num_orig_question})"
        if "llama" in lm_name.lower():
            res_dict["prompt_strs"] = prompt # list[str], len = num_orig_question * n_comb
    return res_dict

# %%
def validate(
        query_tokenizer, query_encoder, language_model, test_dataloader, lm_tokenizer, args, 
        accelerator, model_max_length, train_step_logdir
):
    # %%
    logger.info(f"*** Start validation at {train_step_logdir.split('/')[-1]} ***")
    query_encoder.eval()
    language_model.eval()
    total_ans_prob = 0
    num_batches = len(test_dataloader)
    all_retriever_pick = []
    all_predictions = []
    total_num_correct = 0
    total_num_examples = 0
    total_too_long = 0
    total_has_answer = 0
    total_num_correct_pick = 0
    total_f1_score = 0

    # %%
    for step, raw_batch in tqdm(enumerate(test_dataloader)):
        # %%
        # make raw_batch into a extened batch by first extend each item and then collate_fn
        extended_batch = [inloop_extend_item(
            data=x["data"], corpus=x["corpus"], doc_embeddings=x["doc_embeddings"],
            ret_tokenizer=query_tokenizer, query_encoder=query_encoder, args=args
        ) for x in raw_batch]
        batch = inloop_collate_fn(
            samples=extended_batch, ret_tokenizer=query_tokenizer, lm_tokenizer=lm_tokenizer, 
            lm_name=args.lm_model, args=args, mode="eval"
        ) # dict of keys: query_inputs, doc_embeddings, prompt_ans_lm_inputs, full_answers, [prompt_strs]
        del extended_batch, raw_batch
        
        batch["doc_embeddings"] = batch["doc_embeddings"].to(accelerator.device)
        batch["query_inputs"] = {k: v.to(accelerator.device) for k,v in batch["query_inputs"].items()}
        batch["prompt_ans_lm_inputs"] = {k: v.to(accelerator.device) for k,v in batch["prompt_ans_lm_inputs"].items()}
    
        logger.info(f"[validation step {step}/{num_batches}] max_ret_token_len: {batch['query_inputs']['input_ids'].shape[1]}")
        logger.info(f"[validation step {step}/{num_batches}] max_lm_token_len: {batch['prompt_ans_lm_inputs']['input_ids'].shape[1]}")
        
        # %%
        with torch.no_grad():
            ## Metric 1. Loss (X not need here when testing)
            query_embedding = query_encoder(**batch['query_inputs']).pooler_output \
                if "dpr" in args.query_encoder_type \
                else query_encoder(**batch['query_inputs']).last_hidden_state[:,0,:] # [bs,n_dim]
            doc_embedding = batch["doc_embeddings"]
            logger.info(f"[Sent to query encoder] GPU memory used: {torch.cuda.memory_allocated() / 1e6} MB")
            
            single_device_query_num, _ = query_embedding.shape
            single_device_doc_num = doc_embedding.shape[0]

            logger.info("...Waiting for everyone...")
            if accelerator.use_distributed:
                doc_list = [torch.zeros_like(doc_embedding) for _ in range(accelerator.num_processes)]
                dist.all_gather(tensor_list=doc_list, tensor=doc_embedding.contiguous())
                doc_list[dist.get_rank()] = doc_embedding
                doc_embedding = torch.cat(doc_list, dim=0)

                query_list = [torch.zeros_like(query_embedding) for _ in range(accelerator.num_processes)]
                dist.all_gather(tensor_list=query_list, tensor=query_embedding.contiguous())
                query_list[dist.get_rank()] = query_embedding
                query_embedding = torch.cat(query_list, dim=0)

            # convert query_embedding and doc_embedding to unit vectors
            query_embedding = F.normalize(query_embedding, p=2, dim=1) # p: norm type
            retriever_cossim = torch.sum(query_embedding * doc_embedding, dim=1)  # [bs]
            num_orig_question = single_device_query_num // sum([args.k ** i for i in range(args.max_round + 1)]) if args.empty_doc \
                else single_device_query_num // (sum([args.k ** i for i in range(args.max_round + 1)]) - 1)
            retriever_cossim = retriever_cossim.view(num_orig_question, -1)
            logger.info(f"[Got ret cos sim] GPU memory used: {torch.cuda.memory_allocated() / 1e6} MB")

            query_embedding, doc_embedding = query_embedding.to("cpu"), doc_embedding.to("cpu")
            del query_embedding, doc_embedding
            torch.cuda.empty_cache()
            gc.collect()
            logger.info(f"[Emptied embedding cache] GPU memory used: {torch.cuda.memory_allocated() / 1e6} MB")

            # %%
            if "t5" in args.lm_model:
                lm_prob = get_t5_lm_prob(
                    **batch['prompt_ans_lm_inputs'],
                    model=language_model,
                    device=accelerator.device,
                    tokenizer=lm_tokenizer,
                    max_length=model_max_length,
                    max_tokens_to_generate=args.max_tokens_to_generate,
                    num_orig_question=num_orig_question,
                    llm_batch_size=args.eval_llm_batch_size,
                    logger=logger,
                )
            else:
                lm_prob = get_lm_prob(
                    **batch['prompt_ans_lm_inputs'],
                    model=language_model,
                    device=accelerator.device,
                    max_length=model_max_length,
                    max_tokens_to_generate=args.max_tokens_to_generate,
                    num_orig_question=num_orig_question,
                    llm_batch_size=args.eval_llm_batch_size,
                    logger=logger,
                    # tokenizer=lm_tokenizer, # for debugging only
                )
            # logger.info(f"[Got LM prob] GPU memory used: {torch.cuda.memory_allocated() / 1e6} MB")
            
            ## Metric 2. Average answer probability
            # for each question, take idx of max retriever_cossim 
            # get its corresponding lm_prob
            # note. retriever_cossim and lm_prob are both [n_question,n_comb]
            retrievers_pick = torch.argmax(retriever_cossim,dim=1) # [n_question]

            # # %%
            # # ### debug
            for i in range(min(3,num_orig_question)):
                print(f"retriever_cossim: {retriever_cossim[i]}")
                print(f"retriever's pick: {retrievers_pick[i]}")
                print(f"lm_prob: {lm_prob[i]}")
                print(f"retrievers pick lm score: {lm_prob[i][retrievers_pick[i]]}")
                print(f"lm score each question max: {lm_prob[i][torch.argmax(lm_prob[i])]}")
                print(f"Retriever pick == LM pick? {retrievers_pick[i] == torch.argmax(lm_prob[i])}")
            # print(f"softmax retriever score: {F.softmax(retriever_cossim / args.ret_temperature,dim=1)}")
            
            # print(f"lm_prob.shape: {lm_prob.shape}")
            # debug_prompt_ans_lm_inputs = batch['prompt_ans_lm_inputs']['input_ids'].view(num_orig_question, -1, batch['prompt_ans_lm_inputs']['input_ids'].shape[-1])
            # debug_prompt_ans_lm_token_type_ids = batch['prompt_ans_lm_inputs']['token_type_ids'].view(num_orig_question, -1, batch['prompt_ans_lm_inputs']['token_type_ids'].shape[-1])
            # for i in range(num_orig_question):
            #     print(f"softmax retriever score: {F.softmax(retriever_cossim[i] / 0.1, dim=0)}")
            #     print(f"retriever_pick: {retrievers_pick[i]}")
            #     print(f"orig lm prob: {lm_prob[i]}")
            #     print(f"softmax lm prob: {F.softmax(lm_prob[i] / 0.1, dim=0)}")
            #     print(f"token_type_ids: {debug_prompt_ans_lm_token_type_ids[i]}")
            #     # decode each batch['prompt_ans_lm_inputs']
            #     batch_decode_result = lm_tokenizer.batch_decode(debug_prompt_ans_lm_inputs[i], skip_special_tokens=True)
            #     for j, decode_result in enumerate(batch_decode_result):
            #         print(f"{j}th decoded: {decode_result}")
            # print(f"lm_prob: {lm_prob}")
            # print(f"softmax lm score: {F.softmax(lm_prob / args.lm_temperature,dim=1)}")

            # # print(f"retrievers_pick: {retrievers_pick}")
            # print(f"retrievers_pick.shape: {retrievers_pick.shape}")
            # # decode each batch['prompt_ans_lm_inputs']
            # print(f"lm score each question max: {lm_prob[torch.arange(num_orig_question),torch.argmax(lm_prob,dim=1)]}")
            # print(f"retrievers pick lm score: {lm_prob[torch.arange(num_orig_question),retrievers_pick]}")
            # # ### debug
            # %%
            total_num_correct_pick += (retrievers_pick == torch.argmax(lm_prob,dim=1)).sum().item()
            lm_prob = lm_prob[torch.arange(num_orig_question),retrievers_pick] # [n_question]
            total_ans_prob += lm_prob.sum().item() 
            # count how many retriever's pick is the same as lm's pick
            all_retriever_pick.extend(retrievers_pick.tolist())
            retriever_cossim, lm_prob = retriever_cossim.to("cpu"), lm_prob.to("cpu")
            del retriever_cossim, lm_prob
            torch.cuda.empty_cache()
            gc.collect()
            # logger.info(f"[Emptied scoring cache] GPU memory used: {torch.cuda.memory_allocated() / 1e6} MB")

            # ## Metric 3. Exact match
            # reshape batch['prompt_ans_lm_inputs'] to [n_question,n_comb,n_dim]
            # %%
            logger.debug(f'batch["prompt_ans_lm_inputs"]["input_ids"].shape: {batch["prompt_ans_lm_inputs"]["input_ids"].shape}')
            n_comb = batch["prompt_ans_lm_inputs"]["input_ids"].shape[0] // num_orig_question
            logger.debug(f"n_comb: {n_comb}")
            batch['prompt_ans_lm_inputs'] = {
                k: v.view(num_orig_question, -1, v.shape[-1])[torch.arange(num_orig_question),retrievers_pick] \
                for k,v in batch['prompt_ans_lm_inputs'].items()
            } # [n_question,n_comb,n_dim] -> [n_question,n_dim]
            assert batch["prompt_ans_lm_inputs"]["input_ids"].shape[0] == num_orig_question, f"batch['prompt_ans_lm_inputs']['input_ids'].shape[0] ({batch['prompt_ans_lm_inputs']['input_ids'].shape[0]}) != num_orig_question ({num_orig_question})"
            # tmp_str = lm_tokenizer.decode(batch['prompt_ans_lm_inputs']['input_ids'][0], skip_special_tokens=True)
            # logger.debug(f"Picked option: {tmp_str}")

            # only leave the retriever's pick full_answers, all_qid, prompt_strs
            # use it to index full_answers, all_qid, prompt_strs
            if "llama" in args.lm_model.lower():
                batch["prompt_strs"] = [batch["prompt_strs"][pick + i * n_comb] for i, pick in enumerate(retrievers_pick)]
                assert len(batch["prompt_strs"]) == num_orig_question, f"len(batch['prompt_strs']) ({len(batch['prompt_strs'])}) != num_orig_question ({num_orig_question})"

            # %%
            # TODO (優化) llama 好像其實不用 prompt_ans_lm_inputs, 只要 prompt_strs 就好
            batch_result = lm_gen_and_check(
                model=language_model, 
                tokenizer=lm_tokenizer,
                device=accelerator.device,
                accelerator=accelerator,
                max_length=model_max_length,
                prompt_ans_lm_inputs=batch['prompt_ans_lm_inputs'], # for t5
                prompt_strs = batch["prompt_strs"] if "llama" in args.lm_model.lower() else None, # for llama
                all_full_answers=batch["full_answers"],
                max_tokens_to_generate=args.max_tokens_to_generate,
                llm_batch_size=args.eval_llm_batch_size,
                logger=logger,
            )
            total_num_correct += batch_result["num_correct"]
            total_num_examples += batch_result["num_examples"]
            total_too_long += batch_result["too_long"]
            all_predictions.extend(batch_result["predictions"])
            total_has_answer += batch_result["num_has_answer"]
            total_f1_score += batch_result["sum_f1"]

    # %%
    # write retriever pick to train_step_logdir
    with open(os.path.join(train_step_logdir, "retriever_pick.txt"), "w") as f:
        for pick in all_retriever_pick:
            f.write(str(pick) + "\n")
    with open(os.path.join(train_step_logdir, "prediction.json"), "w", encoding='utf-8') as f:
        for item in all_predictions:
            f.write(item + "\n")
    final_result = {
        "avg_prob": total_ans_prob / total_num_examples, # 這裡原本算錯啦! 原本是每個 batch 的 mean 加起來再除以 num_batches
        "exact_match (%)": total_num_correct / total_num_examples * 100,
        "too_long (%)": total_too_long / total_num_examples * 100,
        "has_answer (%)": total_has_answer / total_num_examples * 100, # 這裡原本算錯啦! 原本是所有 comb 的都算 但其實應該只能看選出來的那個
        "retriever_pick_acc (%)": total_num_correct_pick / total_num_examples * 100,
        "f1_score": total_f1_score / total_num_examples,
    }
    logger.info(f"Done {train_step_logdir.split('/')[-1]} step validation.")
    logger.info(f"total_num_examples: {total_num_examples}")
    for k,v in final_result.items():
        logger.info(f"{k}: {v}")
    return final_result

# %%
def main():
    # %%
    args = parse_args()

    # make sure the first checkpoint exists
    first_ckpt = f"{args.ckpt_dir}/{args.runid_to_eval}/checkpoint-{args.eval_steps}.pt"
    assert os.path.exists(first_ckpt), f"first checkpoint {first_ckpt} does not exist"

    set_seed(args.seed)
    kwargs = DistributedDataParallelKwargs(find_unused_parameters=False)
    accelerator = Accelerator(
        # device_placement='cpu' if debug else 'auto',  # Change this line
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        log_with=None if debug else 'wandb',  # Change this line
        mixed_precision='bf16', # turn on bf16
        kwargs_handlers=[kwargs]
    )
    logger.debug("*** IN DEBUG MODE ***")
    logger.info(f"device: {accelerator.device}")
    if "flan" in args.lm_model:
        model_short_name = "flan"
    elif "llama-3" in args.lm_model.lower():
        model_short_name = "llama3"
    elif "llama" in args.lm_model.lower():
        model_short_name = "llama"
    
    if args.resume_training:
        assert os.path.exists(args.resume_path), f"resume_path {args.resume_path} does not exist"
        logger.info(f"Resuming training from {args.resume_path}")
        # init tracker without config
        accelerator.init_trackers(
            project_name="dpr",
            init_kwargs={"wandb":{"id":args.resume_wandb_id, "resume":"must"}},
        )
    else:
        accelerator.init_trackers(
            project_name="dpr", 
            config=args,
            init_kwargs={"wandb":{"name":
                f"(eval {args.runid_to_eval})({args.dataset_name} {args.data_size}) {model_short_name}-{args.max_round}round-{args.k}k-bs({args.per_device_eval_batch_size})({args.eval_llm_batch_size}) query({args.query_encoder_type}) ({args.empty_doc} empty)"}}
        )
    # %%
    if not debug and accelerator.is_local_main_process:
        wandb_tracker = accelerator.get_tracker("wandb")
        LOG_DIR = wandb_tracker.run.dir
        logger.info(f"Logging to {LOG_DIR}...")
        wandb_tracker.run.log_code(".")
        wandb_tracker.run.tags = [
            f"size: {args.data_size}", f"lm: {args.lm_model}", 
            f"query_enc: {args.query_encoder}",
            f"max_round: {args.max_round}", f"k: {args.k}", 
            f"eval_bs: {args.per_device_eval_batch_size}",
            "newline_format_prompt",
            f"empty_doc: {args.empty_doc}",
            "cossim_ret_score (correct)", 
            f"id: {args.runid_to_eval}", "only_eval"
        ]
    else:
        LOG_DIR = "./tmp_log"  # Or any other directory you want to use when debugging
    
    # %%
    query_tokenizer, query_encoder = load_query_encoder_and_tokenizer(args, logger)

    if not debug and accelerator.is_local_main_process:
        wandb_tracker.run.watch(query_encoder, log_freq=500)
    # %%

    logger.info("...Loading language models...")
    language_model, lm_tokenizer, lm_config = load_lm_model_and_tokenizer(
        args.lm_model, device=accelerator.device, quantized=args.quantized,
        model_parallelism=args.model_parallelism, cache_dir=args.cache_dir, auth_token=args.auth_token
    )
    language_model.eval()

    model_max_length = lm_config.n_positions if hasattr(lm_config, "n_positions") else lm_config.max_position_embeddings
    # only pad if model is gpt2
    if "gpt2" in args.lm_model or "llama" in args.lm_model:
        lm_tokenizer.pad_token = lm_tokenizer.eos_token
        # lm_tokenizer.pad_token = "[PAD]"
        lm_tokenizer.padding_side = "left" # TODO in llama1, should pad left??
    logger.info(f"GPU memory used: {torch.cuda.memory_allocated() / 1e6} MB")

    if args.data_size == "debug":
        test_size = 10
    elif args.data_size == "1000":
        test_size = 1000
    elif args.data_size == "full":
        if args.dataset_name == "nq":
            test_size = 3610
        elif args.dataset_name == "trivia":
            raise NotImplementedError("Trivia full size is not available.")
        elif args.dataset_name == "hotpot":
            raise NotImplementedError("Hotpot full size is not available.")
        else: 
            raise ValueError(f"Invalid dataset_name: {args.dataset_name}")
    else:
        raise ValueError(f"Invalid data_size: {args.data_size}")
    args.test_file = args.test_file.replace(".json", f".size-{test_size}.json")
    logger.info("...Loading data...")
    test_data = json.load(open(os.path.join(args.test_dir, args.test_file)))
    logger.info(f"Size of test data: {len(test_data)}")

    logger.info("...Creating Corpus...")
    test_corpus = [[x['text'] for x in sample['ctxs']] for sample in test_data]
    logger.info(f"Size of test corpus: {len(test_corpus)}")

    index_dir = os.path.join(args.base_index_dir, args.doc_encoder_type)
    index_path = {
        "test": os.path.join(index_dir, f"test_{test_size}_norm.pt"),
        "empty_doc": os.path.join(index_dir, "empty_doc_norm.pt")
    }

    if all([os.path.exists(path) for path in index_path.values()]):
        logger.info(f"...Loading index from {index_path.values()}...") 
        doc_embeddings = {
            "test": torch.load(index_path["test"]),
            "empty_doc": torch.load(index_path["empty_doc"])
        }
        assert len(doc_embeddings['test']) == len(test_corpus), f"len(doc_embeddings['test']) ({len(doc_embeddings['test'])}) != len(test_corpus), ({len(test_corpus)})"
    else:
        for split, path in index_path.items():
            if not os.path.exists(path):
                raise ValueError(f"{split} Index file {path} not found. Please prepcoess_idx.py first.")

    # check if the norm is correct
    for split, emb_list in doc_embeddings.items():
        # only check the first one
        print("Checking norm of ", split)
        emb = emb_list[0] if split != "empty_doc" else emb_list
        print(f"Shape: {emb.shape}")
        assert torch.allclose(torch.sum(emb**2, dim=-1), torch.ones(emb.shape[0]), atol=1e-5), f"Norm of {split} is not correct. Shape: {emb.shape}. Norm: {torch.sum(emb**2, dim=1)}"

    # take the [args.num_exemplars:] 
    test_data = test_data[args.num_exemplars:]
    test_corpus = test_corpus[args.num_exemplars:]
    doc_embeddings['test'] = doc_embeddings['test'][args.num_exemplars:]

    # TODO add feature of empty doc representation

    # Answer is a LIST instead of a str
    # In test set, keep track of full answer list of each question
    test_qa_pairs = [(normalize_query(sample['question']), [""], sample['answers'], doc_embeddings['empty_doc']) for sample in test_data]

    logger.info(f"len(test_qa_pairs): {len(test_qa_pairs)}")
    logger.info(f"len(test_corpus): {len(test_corpus)}")
    logger.info(f"len(doc_embeddings['test']): {len(doc_embeddings['test'])}")

    logger.info("...Build Dataset & Dataloader...")
    query_encoder = accelerator.prepare(query_encoder)
    logger.info(f"query_encoder is on {query_encoder.device}")
    test_dataset = QADataset(test_qa_pairs, test_corpus, doc_embeddings['test'])
    
    logger.info("...Deleting train_data and test_data...")
    del test_data

    test_dataloader = torch.utils.data.DataLoader(test_dataset,batch_size=args.per_device_eval_batch_size,shuffle=False,collate_fn=test_dataset.collate_fn,num_workers=args.num_workers,pin_memory=args.pin_memory)
    logger.info(f"GPU memory used: {torch.cuda.memory_allocated() / 1e6} MB")
    
    logger.info("...Prepare accelerator...")
    test_dataloader, language_model = accelerator.prepare(
        test_dataloader, language_model 
    )
    logger.info(f"GPU memory used: {torch.cuda.memory_allocated() / 1e6} MB")
    start_time = time.time()

    logger.info("\n***** Running testing *****")
    logger.info(f"Test file: {args.test_file}")
    logger.info(f"Embedding path: {index_path['test']} & {index_path['empty_doc']}")
    logger.info(f"Test size: {args.data_size}")
    logger.info(f"Model: {args.lm_model}")
    logger.info(f"Query encoder: {args.query_encoder}")
    logger.info(f"Doc encoder: {args.doc_encoder_type}")

    for completed_steps in range(0, args.max_eval_steps+1, args.eval_steps):
    # for completed_steps in range(0, MAX_TRAIN_STEPS, EVAL_STEPS):
        logger.info(f"...{completed_steps} Step Evaluation...")
        steps_log_dir = os.path.join(LOG_DIR,f"step-{completed_steps}")
        if not os.path.exists(steps_log_dir):
            os.makedirs(steps_log_dir)
        if completed_steps > 0:
            # Load old query encoder ckpt
            args.query_encoder = os.path.join(f"{args.ckpt_dir}/{args.runid_to_eval}", f"checkpoint-{completed_steps}.pt")
            logger.info(f"...Loading old state_dict from ckpt {args.query_encoder}...")
            state_dict = torch.load(args.query_encoder)
            query_encoder.load_state_dict(state_dict["query_encoder"])
            loaded_completed_steps = state_dict["completed_steps"]
            assert loaded_completed_steps == completed_steps, f"loaded_completed_steps ({loaded_completed_steps}) != completed_steps ({completed_steps})"
            logger.info(f"...State_dict at step {completed_steps} loaded to query_encoder, optimizer, lr_scheduler...")

        query_encoder.eval()
        eval_result = validate(query_tokenizer, query_encoder, language_model, test_dataloader, lm_tokenizer, args, accelerator, model_max_length, steps_log_dir)
        accelerator.log({"eval":eval_result}, step=completed_steps)
        # query_encoder = query_encoder.to("cpu")
        # del query_encoder
        # torch.cuda.empty_cache()
        # gc.collect()

    if accelerator.is_local_main_process:
        logger.info(f"max_ret_token_len: {max_ret_token_len}; max_lm_token_len: {max_lm_token_len}")
        logger.info(f"Time spent: {time.time() - start_time} seconds")
        logger.info(f"Max GPU memory used: {torch.cuda.max_memory_allocated() / 1e6} MB")
        logger.info("...!!Congrats!! Evaluation finished :) ...")
        if not debug:
            wandb_tracker.finish()
    

# %%
if __name__ == '__main__':
    main()
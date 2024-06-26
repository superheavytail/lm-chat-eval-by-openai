from pathlib import Path
import os
import fire
import jsonlines
import json

from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline
from tqdm import tqdm
from batched_chatgpt import call_chatgpt
import torch


def main(
    model_name,
    save_name,
    eval_set_path,
    output_dir,
    model_type,
    debug: bool = False
):
    # check if save path is safe
    save_path = Path(output_dir) / save_name
    save_path = save_path.with_suffix(".jsonl")
    if save_path.exists():
        raise FileExistsError(f"{save_path} exists!")

    print("=== model name ===")
    print(model_name)
    print("===    ====    ===")
    print(os.getcwd())

    # prepare evaluation set
    eval_set = [e for e in jsonlines.open(eval_set_path).iter()]
    eval_set = eval_set[:12] if debug else eval_set

    generated_answers = []

    if model_type == 'openai':
        input_texts = [f"{e['instruction']}\n\n{e['instances'][0]['input']}" for e in eval_set]
        generated_answers = call_chatgpt(input_texts, chunk_size=20, model_name=model_name)
    else:
        # for multi-GPU inference
        try:
            print(f"{os.environ['LOCAL_RANK']=}")
            print(f"{os.environ['WORLD_SIZE']=}")
            local_rank = int(os.environ['LOCAL_RANK'])
            world_size = int(os.environ['WORLD_SIZE'])
            eval_set = eval_set[local_rank::world_size]
            save_path = (save_path.parent / (save_path.stem + f'-rank{local_rank}')).with_suffix('.jsonl')
            device = f"cuda:{local_rank}"
        except KeyError:
            device = "cuda"

        # This models need special generation strategy
        if model_type in ['solar', 'mistral', 'kullm', 'koalpaca_v1_1b', 'hyundai_llm']:
            # There exists more simple way, but let us move fast.
            # model, tokenizer load
            try:
                model = AutoModelForCausalLM.from_pretrained(
                    model_name,
                    torch_dtype='auto',
                    attn_implementation='sdpa').to(device)
            except ValueError:
                model = AutoModelForCausalLM.from_pretrained(
                    model_name,
                    torch_dtype='auto').to(device)
            tokenizer = AutoTokenizer.from_pretrained(model_name)

            # building input text
            # solar uses apply_chat_template but kullm doesn't
            if model_type == 'solar' or model_type == 'mistral':
                input_texts = [f"{e['instruction']}\n\n{e['instances'][0]['input']}" for e in eval_set]
                input_template_texts = [tokenizer.apply_chat_template(
                    [{"role": "user", "content": t}], tokenize=False, add_generation_prompt=True) for t in input_texts]

            elif model_type in ['kullm', 'koalpaca_v1_1b', 'hyundai_llm']:
                # for generate
                model.config.pad_token_id = model.config.eos_token_id

                with open(f"lm_templates/{model_type}.json", 'rt') as f:
                    template = json.load(f)

                input_template_texts = []
                for e in eval_set:
                    if e['instances'][0]['input']:
                        input_text = template['prompt_input'].format_map({
                            'instruction': e['instruction'],
                            'input': e['instances'][0]['input']
                        })
                    else:
                        input_text = template['prompt_no_input'].format_map({
                            'instruction': e['instruction'],
                        })
                    input_template_texts.append(input_text)
            else:
                raise NotImplementedError

            input_ids = [tokenizer(e, return_tensors='pt')["input_ids"].to(model.device) for e in input_template_texts]

            # do generation
            if not tokenizer.pad_token_id and tokenizer.eos_token_id:
                tokenizer.pad_token_id = tokenizer.eos_token_id
            for item in tqdm(input_ids):
                res = model.generate(
                    item,
                    use_cache=True,
                    max_new_tokens=min(2048-len(item[0]), 512),
                    eos_token_id=tokenizer.eos_token_id,
                    pad_token_id=tokenizer.pad_token_id,
                    do_sample=False,
                )
                try:
                    model_answer = tokenizer.decode(res[0][len(item[0]):], skip_special_tokens=True)
                except IndexError:
                    print("model generate nothing!! Full model answer:")
                    model_answer = "(no answer from model)"  # Some models rarely generate nothing.

                generated_answers.append(model_answer)
                if debug:
                    print(model_answer)
        else:
            print(f"model_type: {model_type}, use default chat template.")
            model = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype="auto", attn_implementation='sdpa')
            tokenizer = AutoTokenizer.from_pretrained(model_name)
            pipe = pipeline("text-generation", model=model, tokenizer=tokenizer, device=device, return_full_text=False,
                            do_sample=False, use_cache=True)
            for e in tqdm(eval_set):
                messages = [{'role': 'user', 'content': f"{e['instruction']}\n\n{e['instances'][0]['input']}"}]
                inputs = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
                generated_answers.append(pipe(inputs, max_new_tokens=2048)[0]['generated_text'])
                print(generated_answers[-1])


    # saving results
    # zip instruction, input, answer
    results = []
    for eval_item, answer in zip(eval_set, generated_answers):
        results.append({
            'instruction': eval_item['instruction'],
            'input': eval_item['instances'][0]['input'],
            'answer': answer
        })

    if not debug:
        with open(save_path, "wt", encoding="utf-8") as f:
            for result in results:
                json.dump(result, f, ensure_ascii=False)  # set ensure_ascii False for Korean language
                f.write("\n")  # since it's jsonl

    if debug:
        for result in results:
            print(f"{result['instruction']=}")
            print(f"{result['input']=}")
            print(f"{result['answer']=}")


if __name__ == '__main__':
    fire.Fire(main)

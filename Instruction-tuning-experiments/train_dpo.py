import sys
import os
import torch
import numpy as np
from logging import warning
from datasets import DatasetDict
from argparse import ArgumentParser

from transformers import (
    AutoTokenizer,
    TrainingArguments,
)

from trl import (
    DPOTrainer,
    # create_reference_model
)

# custom classes
from utils import load_model, get_peft_config
from dpo_finetuning_datasets import read_data_dpo

model_max_length = 2048

def argparser():
    ap = ArgumentParser()
    ap.add_argument('--deepspeed_config', type=str, default="./ds-configs/oa_deepspeed_rl_zero3_warmuplr.json")
    ap.add_argument('--learning_rate', type=float, default=2e-5)
    ap.add_argument('--model', type=str)
    ap.add_argument('--tokenizer', type=str)
    ap.add_argument('--num_train_epochs', type=int, default=1)
    ap.add_argument('--per_device_batch_size', type=int, default=1)
    ap.add_argument('--output_dir', type=str, default="output")
    ap.add_argument('--gradient_accumulation_steps', type=int, default=4)
    ap.add_argument('--output_file', type=str)
    ap.add_argument('--training_data', type=str, default="oasst")
    ap.add_argument('--lang', type=str, default="en")
    ap.add_argument('--local_rank', type=int)
    ap.add_argument('--use_lora', default=True, type=lambda x: (str(x).lower() == 'true'))
    ap.add_argument('--lora_r', type=int, default=16)
    ap.add_argument('--max_examples', type=int, default=None)
    ap.add_argument('--transformers_cache',type=str, default="/scratch/project_462000319/transformers_cache")
    ap.add_argument('--dropout',type=float, default=0.1)
    return ap

def preprocess_dpo(data):  
    prompts = data['prompt']
    contexts = data['context']
    accepted = data['accepted_response']
    rejected = data['rejected_response']
    dpo_dataset = {
        "prompt": [],
        "chosen": [],
        "rejected": []
    }
    for prompt, context, accepted, rejected in zip(prompts, contexts, accepted, rejected):
        if not context or context.isspace():
            combined_prompt = prompt
        else:
            combined_prompt = context + "\n" + prompt
        dpo_dataset["prompt"].append(combined_prompt)
        dpo_dataset["chosen"].append(accepted)
        dpo_dataset["rejected"].append(rejected)
    return dpo_dataset

def train_dpo(args):
    # https://github.com/huggingface/trl/blob/main/examples/scripts/dpo.py
    log_dir = './logs/'
    base_model_name = os.path.basename(args.model)
    output_dir = os.path.join("../../models/dpo_checkpoints/", base_model_name + "-" + args.training_data + "-" + args.lang)
    print("Saving checkpoints to", output_dir)

    # This needs to be defined before model loading for deepspeed stage 3 to work correctly
    # initialize training arguments
    training_args = TrainingArguments(
        deepspeed=args.deepspeed_config,
        remove_unused_columns=False,
        output_dir=output_dir,
        logging_dir=log_dir,
        evaluation_strategy="steps",
        eval_steps=300,
        num_train_epochs=args.num_train_epochs,
        save_strategy="steps",
        save_steps=100,
        save_total_limit=5,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=4,
        log_on_each_node=False,
        logging_strategy="steps",
        logging_steps=10,
        logging_first_step=True,
        report_to='tensorboard',
        learning_rate=args.learning_rate,
        optim="rmsprop",
        warmup_ratio=0.1,
        bf16=True,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        half_precision_backend="cuda_amp",
        local_rank=args.local_rank,
    )

    # TOKENIZER
    print("tokenizer :", args.tokenizer)
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)

    # MODEL
    # 1. load a pretrained model
    print("base model:", args.model)
    print("=== Loading model ===")
    model = load_model(args.model, args.transformers_cache, use_lora=False, lora_r=args.lora_r)

    # print("=== Loading model_ref ===")
    # model_ref = load_model(args.model, args.transformers_cache, args.use_lora)
    # print("model_ref device:", model_ref.device)
    # create_reference_model error: DeepSpeed ZeRO-3 is enabled and is not compatible with `create_reference_model()
    # model_ref = create_reference_model(model, num_shared_layers=6)

    # 2-3. Load training/valid/eval datasets
    print("load train_data")
    train_data = read_data_dpo(args.training_data, split="train", lang=args.lang, max_examples=args.max_examples)
    print("load val_data")
    val_data = read_data_dpo(args.training_data, split="valid", lang=args.lang, max_examples=args.max_examples)
    print("load eval_data")
    eval_data = read_data_dpo(args.training_data, split="eval", lang=args.lang, max_examples=args.max_examples)

    print("Size of training data", len(train_data))
    print("Size of validation data", len(val_data))
    print("Size of evaluation data", len(eval_data))

    dataset = DatasetDict({
        'train': train_data,
        'validation': val_data,
        'evaluation': eval_data,
    })

    dataset = dataset.map(
        lambda d: preprocess_dpo(d),
        batched=True
    )

    # print("Filtering by length")
    # dataset = filter_by_length(dataset, model_max_length)

    print("Size of training data", len(dataset['train']))

    # 5. initialize the DPO trainer
    dpo_trainer = DPOTrainer(
        model=model,
        ref_model=None,
        args=training_args,
        beta=0.1,
        train_dataset=dataset['train'],
        eval_dataset=dataset['validation'],
        tokenizer=tokenizer,
        max_length=model_max_length,
        max_target_length=256,
        max_prompt_length=256,
        padding_value=tokenizer.pad_token_id,
        peft_config=get_peft_config(args),
    )

    # 6. train
    dpo_trainer.train()

    base_model_name = os.path.basename(args.model)
    save_directory = os.path.join("../../models/dpo_finetuned/", base_model_name + "-" + args.training_data + "-" + args.lang)
    dpo_trainer.save_model(save_directory)
    eval_results = dpo_trainer.evaluate(dataset['evaluation'])

    print('Training data', args.training_data)
    print('Model:', args.model)
    print('Learning rate:', args.learning_rate)
    print('batch size:', args.per_device_batch_size)
    print('Gradient accumulation steps:', args.gradient_accumulation_steps)
    print('Evaluation results:', eval_results['eval_loss'])
    print('Save directory:', save_directory)

def main(argv):
    args = argparser().parse_args(argv[1:])
    train_dpo(args)

if __name__ == '__main__':
    sys.exit(main(sys.argv))
# adapted from run_glue.py of huggingface transformers

import dataclasses
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, Optional
import unittest
import numpy as np

from transformers import (
    AutoConfig,
    AutoModelForSequenceClassification,
    AutoTokenizer,
    EvalPrediction,
    GlueDataset,
    GlueDataTrainingArguments,
    TrainingArguments,
    glue_compute_metrics,
    glue_output_modes,
    glue_tasks_num_labels,
    set_seed,
)

import onnxruntime
from onnxruntime.capi.ort_trainer import ORTTrainer, LossScaler, ModelDescription, IODescription

from orttraining_transformer_trainer import ORTTransformerTrainer

import torch

logger = logging.getLogger(__name__)

@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune from.
    """

    model_name_or_path: str = field(
        metadata={"help": "model identifier from huggingface.co/models"}
    )
    config_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"}
    )
    tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained tokenizer name or path if not the same as model_name"}
    )
    cache_dir: Optional[str] = field(
        default=None, metadata={"help": "Where do you want to store the pretrained models downloaded from s3"}
    )

class ORTGlueTest(unittest.TestCase):

    def setUp(self):
        # configurations not to be changed accoss tests
        self.max_seq_length = 128
        self.train_batch_size = 8
        self.learning_rate = 2e-5
        self.num_train_epochs = 3.0
        self.local_rank = -1
        self.overwrite_output_dir = True
        self.gradient_accumulation_steps = 1
        self.data_dir = "/bert_data/hf_data/glue_data/"
        self.output_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "glue_test_output/")
        self.cache_dir = '/tmp/glue/'
        self.logging_steps = 10

    def test_bert_with_mrpc(self):
        results = self.run_glue(model_name="bert-base-cased", task_name="MRPC", fp16=False)
        self.assertTrue(results['acc'] > 0.84)
        self.assertTrue(results['f1'] > 0.88)
        self.assertTrue(results['acc_and_f1'] > 0.86)
        self.assertTrue(results['loss'] < 0.47)

    def test_bert_fp16_with_mrpc(self):
        results = self.run_glue(model_name="bert-base-cased", task_name="MRPC", fp16=True)
        self.assertTrue(results['acc'] > 0.85)
        self.assertTrue(results['f1'] > 0.89)
        self.assertTrue(results['acc_and_f1'] > 0.87)
        self.assertTrue(results['loss'] < 0.46)

    def run_glue(self, model_name, task_name, fp16):
        model_args = ModelArguments(model_name_or_path=model_name, cache_dir=self.cache_dir)
        data_args = GlueDataTrainingArguments(task_name=task_name, data_dir=self.data_dir + "/" + task_name,
            max_seq_length=self.max_seq_length)
            
        training_args = TrainingArguments(output_dir=self.output_dir + "/" + task_name, do_train=True, do_eval=True,
            per_gpu_train_batch_size=self.train_batch_size,
            learning_rate=self.learning_rate, num_train_epochs=self.num_train_epochs,local_rank=self.local_rank,
            overwrite_output_dir=self.overwrite_output_dir, gradient_accumulation_steps=self.gradient_accumulation_steps,
            fp16=fp16, logging_steps=self.logging_steps)

        # Setup logging
        logging.basicConfig(
            format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
            datefmt="%m/%d/%Y %H:%M:%S",
            level=logging.INFO if training_args.local_rank in [-1, 0] else logging.WARN,
        )
        logger.warning(
            "Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
            training_args.local_rank,
            training_args.device,
            training_args.n_gpu,
            bool(training_args.local_rank != -1),
            training_args.fp16,
        )
        logger.info("Training/evaluation parameters %s", training_args)

        set_seed(training_args.seed)
        onnxruntime.set_seed(training_args.seed)

        try:
            num_labels = glue_tasks_num_labels[data_args.task_name]
            output_mode = glue_output_modes[data_args.task_name]
        except KeyError:
            raise ValueError("Task not found: %s" % (data_args.task_name))

        config = AutoConfig.from_pretrained(
            model_args.config_name if model_args.config_name else model_args.model_name_or_path,
            num_labels=num_labels,
            finetuning_task=data_args.task_name,
            cache_dir=model_args.cache_dir,
        )
        tokenizer = AutoTokenizer.from_pretrained(
            model_args.tokenizer_name if model_args.tokenizer_name else model_args.model_name_or_path,
            cache_dir=model_args.cache_dir,
        )
        model = AutoModelForSequenceClassification.from_pretrained(
            model_args.model_name_or_path,
            from_tf=bool(".ckpt" in model_args.model_name_or_path),
            config=config,
            cache_dir=model_args.cache_dir,
        )

        train_dataset = (
            GlueDataset(data_args, tokenizer=tokenizer)
            if training_args.do_train
            else None
        )

        print(data_args)
        print(training_args.local_rank)
        eval_dataset = (
            GlueDataset(data_args, tokenizer=tokenizer, mode="dev")
            if training_args.do_eval
            else None
        )

        def compute_metrics(p: EvalPrediction) -> Dict:
            if output_mode == "classification":
                preds = np.argmax(p.predictions, axis=1)
            elif output_mode == "regression":
                preds = np.squeeze(p.predictions)
            return glue_compute_metrics(data_args.task_name, preds, p.label_ids)

        model_desc = ModelDescription([
            IODescription('input_ids', ['batch', 'max_seq_len_in_batch'], torch.int64, num_classes=model.config.vocab_size),
            IODescription('attention_mask', ['batch', 'max_seq_len_in_batch'], torch.int64, num_classes=2),
            IODescription('token_type_ids', ['batch', 'max_seq_len_in_batch'], torch.int64, num_classes=2),
            IODescription('labels', ['batch',], torch.int64, num_classes=2)], [
            IODescription('loss', [], torch.float32),
            IODescription('logits', ['batch', 2], torch.float32)])

        # Initialize the ORTTrainer within ORTTransformerTrainer
        trainer = ORTTransformerTrainer(
            model=model,
            model_desc=model_desc,
            args=training_args,
            train_dataset=train_dataset,
            eval_dataset=eval_dataset,
            compute_metrics=compute_metrics,
        )

        # Training
        if training_args.do_train:
            trainer.train()
            trainer.save_model()

        # Evaluation
        results = {}
        if training_args.do_eval and training_args.local_rank in [-1, 0]:
            logger.info("*** Evaluate ***")

            result = trainer.evaluate()

            logger.info("***** Eval results {} *****".format(data_args.task_name))
            for key, value in result.items():
               logger.info("  %s = %s", key, value)

            results.update(result)

        return results

if __name__ == "__main__":
    unittest.main()

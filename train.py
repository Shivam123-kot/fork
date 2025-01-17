"""
Donut
Copyright (c) 2022-present NAVER Corp.
MIT License
Copyright (c) Meta Platforms, Inc. and affiliates.
"""
import argparse
import datetime
import os
from os.path import basename
from pathlib import Path

import pytorch_lightning as pl
import torch
from pytorch_lightning.callbacks import LearningRateMonitor, ModelCheckpoint, Callback
from pytorch_lightning.loggers.tensorboard import TensorBoardLogger
from pytorch_lightning.plugins import CheckpointIO
from pytorch_lightning.plugins.environments import SLURMEnvironment
from pytorch_lightning.utilities import rank_zero_only
from sconf import Config

from nougat import NougatDataset
from lightning_module import NougatDataPLModule, NougatModelPLModule

try:
    import wandb
    from pytorch_lightning.loggers import WandbLogger as Logger
except ModuleNotFoundError:
    from pytorch_lightning.loggers.tensorboard import TensorBoardLogger as Logger

import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class CustomCheckpointIO(CheckpointIO):
    def save_checkpoint(self, checkpoint, path, storage_options=None):
        torch.save(checkpoint, path)

    def load_checkpoint(self, path, storage_options=None):
        path = Path(path)
        if path.is_file():
            print("path:", path, path.is_dir())
            ckpt = torch.load(path)
            if not "state_dict" in ckpt:
                ckpt["state_dict"] = {
                    "model." + key: value
                    for key, value in torch.load(
                        path.parent / "pytorch_model.bin"
                    ).items()
                }
            return ckpt
        else:
            checkpoint = torch.load(path / "artifacts.ckpt")
            state_dict = torch.load(path / "pytorch_model.bin")
            checkpoint["state_dict"] = {
                "model." + key: value for key, value in state_dict.items()
            }
            return checkpoint

    def remove_checkpoint(self, path) -> None:
        return super().remove_checkpoint(path)


class GradNormCallback(Callback):
    """
    Logs the gradient norm.
    """

    @staticmethod
    def gradient_norm(model):
        total_norm = 0.0
        for p in model.parameters():
            if p.grad is not None:
                param_norm = p.grad.detach().data.norm(2)
                total_norm += param_norm.item() ** 2
        total_norm = total_norm**0.5
        return total_norm

    def on_after_backward(self, trainer, model):
        model.log("train/grad_norm", self.gradient_norm(model))


@rank_zero_only
def save_config_file(config, path):
    if not Path(path).exists():
        os.makedirs(path)
    save_path = Path(path) / "config.yaml"
    print(config.dumps())
    with open(save_path, "w") as f:
        f.write(config.dumps(modified_color=None, quote_str=True))
        print(f"Config is saved at {save_path}")


def train(config):
    pl.utilities.seed.seed_everything(config.get("seed", 42), workers=True)

    model_module = NougatModelPLModule(config)
    data_module = NougatDataPLModule(config)

    # add datasets to data_module
    datasets = {"train": [], "validation": []}
    for i, dataset_path in enumerate(config.dataset_paths):
        for split in ["train", "validation"]:
            datasets[split].append(
                NougatDataset(
                    dataset_path=dataset_path,
                    nougat_model=model_module.model,
                    max_length=config.max_length,
                    split=split,
                )
            )
    data_module.train_datasets = datasets["train"]
    data_module.val_datasets = datasets["validation"]

    lr_callback = LearningRateMonitor(logging_interval="step")

    checkpoint_callback = ModelCheckpoint(
        save_last=True,
        dirpath=Path(config.result_path) / config.exp_name / config.exp_version,
    )
    grad_norm_callback = GradNormCallback()
    custom_ckpt = CustomCheckpointIO()

    if not config.debug:
        logger = Logger(config.exp_name, project="Nougat", config=dict(config))
    else:
        logger = TensorBoardLogger(
            save_dir=config.result_path,
            name=config.exp_name,
            version=config.exp_version,
            default_hp_metric=False,
        )
    trainer = pl.Trainer(
        resume_from_checkpoint=config.get("resume_from_checkpoint_path", None),
        num_nodes=config.get("num_nodes", 1),
        gpus=torch.cuda.device_count(),
        strategy="ddp",
        accelerator="gpu",
        plugins=[custom_ckpt, SLURMEnvironment(auto_requeue=False)],
        max_epochs=config.max_epochs,
        max_steps=config.max_steps,
        val_check_interval=config.val_check_interval,
        check_val_every_n_epoch=config.check_val_every_n_epoch,
        limit_val_batches=config.val_batches,
        gradient_clip_val=config.gradient_clip_val,
        accumulate_grad_batches=config.accumulate_grad_batches,
        log_every_n_steps=15,
        precision="bf16",
        num_sanity_val_steps=0,
        logger=logger,
        callbacks=[lr_callback, checkpoint_callback, grad_norm_callback],
    )

    trainer.fit(model_module, data_module)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, required=True)
    parser.add_argument("--exp_version", type=str, required=False)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--job", type=int, default=None)
    args, left_argv = parser.parse_known_args()

    config = Config(args.config)
    config.argv_update(left_argv)
    config.debug = args.debug
    config.job = args.job
    if not config.get("exp_name", False):
        config.exp_name = basename(args.config).split(".")[0]
    config.exp_version = (
        datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        if not args.exp_version
        else args.exp_version
    )

    save_config_file(
        config, Path(config.result_path) / config.exp_name / config.exp_version
    )
    train(config)

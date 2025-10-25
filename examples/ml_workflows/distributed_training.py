"""Distributed Training Workflow Example for Flyte

This module demonstrates how to implement distributed training workflows
using Flyte with PyTorch DistributedDataParallel for scalable model training.
"""

import os
from dataclasses import dataclass
from typing import Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import DataLoader, DistributedSampler
from torchvision import datasets, transforms

from flytekit import task, workflow, Resources, ImageSpec
from flytekit.types.directory import FlyteDirectory
from flytekit.types.file import FlyteFile


# Define custom image with required dependencies
image_spec = ImageSpec(
    name="distributed-training",
    packages=[
        "torch==2.1.0",
        "torchvision==0.16.0",
        "pandas==2.1.0",
        "scikit-learn==1.3.0",
    ],
    registry="ghcr.io/swamy18",
)


@dataclass
class TrainingConfig:
    """Configuration for distributed training"""
    batch_size: int = 64
    test_batch_size: int = 1000
    epochs: int = 10
    learning_rate: float = 0.01
    momentum: float = 0.5
    seed: int = 42
    log_interval: int = 100
    num_workers: int = 4


class ConvNet(nn.Module):
    """Simple convolutional neural network for image classification"""
    
    def __init__(self, num_classes: int = 10):
        super(ConvNet, self).__init__()
        self.conv1 = nn.Conv2d(1, 32, 3, 1)
        self.conv2 = nn.Conv2d(32, 64, 3, 1)
        self.dropout1 = nn.Dropout(0.25)
        self.dropout2 = nn.Dropout(0.5)
        self.fc1 = nn.Linear(9216, 128)
        self.fc2 = nn.Linear(128, num_classes)

    def forward(self, x):
        x = self.conv1(x)
        x = F.relu(x)
        x = self.conv2(x)
        x = F.relu(x)
        x = F.max_pool2d(x, 2)
        x = self.dropout1(x)
        x = torch.flatten(x, 1)
        x = self.fc1(x)
        x = F.relu(x)
        x = self.dropout2(x)
        x = self.fc2(x)
        return F.log_softmax(x, dim=1)


@task(
    requests=Resources(cpu="4", mem="8Gi", gpu="1"),
    limits=Resources(cpu="6", mem="12Gi", gpu="1"),
    cache=True,
    cache_version="1.0",
    container_image=image_spec,
)
def prepare_data(data_dir: str = "/data") -> FlyteDirectory:
    """Download and prepare training data
    
    Args:
        data_dir: Directory to store downloaded data
        
    Returns:
        FlyteDirectory containing prepared datasets
    """
    transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize((0.1307,), (0.3081,))
    ])
    
    # Download training data
    datasets.MNIST(
        data_dir,
        train=True,
        download=True,
        transform=transform
    )
    
    # Download test data
    datasets.MNIST(
        data_dir,
        train=False,
        download=True,
        transform=transform
    )
    
    return FlyteDirectory(path=data_dir)


@task(
    requests=Resources(cpu="8", mem="16Gi", gpu="2"),
    limits=Resources(cpu="12", mem="24Gi", gpu="2"),
    cache=True,
    cache_version="1.0",
    container_image=image_spec,
)
def train_distributed(
    data_dir: FlyteDirectory,
    config: TrainingConfig,
    world_size: int = 2,
) -> Tuple[FlyteFile, dict]:
    """Train model using distributed data parallel
    
    Args:
        data_dir: Directory containing training data
        config: Training configuration
        world_size: Number of processes for distributed training
        
    Returns:
        Tuple of trained model file and training metrics
    """
    torch.manual_seed(config.seed)
    
    # Setup distributed training
    rank = int(os.environ.get("RANK", 0))
    local_rank = int(os.environ.get("LOCAL_RANK", 0))
    
    if torch.cuda.is_available():
        torch.cuda.set_device(local_rank)
        device = torch.device(f"cuda:{local_rank}")
    else:
        device = torch.device("cpu")
    
    # Initialize distributed process group
    if world_size > 1:
        torch.distributed.init_process_group(
            backend="nccl" if torch.cuda.is_available() else "gloo",
            init_method="env://",
            world_size=world_size,
            rank=rank,
        )
    
    # Load data with distributed sampler
    transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize((0.1307,), (0.3081,))
    ])
    
    train_dataset = datasets.MNIST(
        data_dir.path,
        train=True,
        transform=transform
    )
    
    train_sampler = DistributedSampler(
        train_dataset,
        num_replicas=world_size,
        rank=rank
    ) if world_size > 1 else None
    
    train_loader = DataLoader(
        train_dataset,
        batch_size=config.batch_size,
        sampler=train_sampler,
        num_workers=config.num_workers,
        pin_memory=True,
    )
    
    # Initialize model
    model = ConvNet().to(device)
    
    if world_size > 1:
        model = nn.parallel.DistributedDataParallel(
            model,
            device_ids=[local_rank] if torch.cuda.is_available() else None
        )
    
    optimizer = optim.SGD(
        model.parameters(),
        lr=config.learning_rate,
        momentum=config.momentum
    )
    
    # Training loop
    metrics = {
        "train_loss": [],
        "epoch_times": [],
    }
    
    for epoch in range(1, config.epochs + 1):
        if train_sampler:
            train_sampler.set_epoch(epoch)
        
        model.train()
        epoch_loss = 0.0
        
        for batch_idx, (data, target) in enumerate(train_loader):
            data, target = data.to(device), target.to(device)
            
            optimizer.zero_grad()
            output = model(data)
            loss = F.nll_loss(output, target)
            loss.backward()
            optimizer.step()
            
            epoch_loss += loss.item()
            
            if batch_idx % config.log_interval == 0 and rank == 0:
                print(
                    f"Train Epoch: {epoch} "
                    f"[{batch_idx * len(data)}/{len(train_loader.dataset)} "
                    f"({100. * batch_idx / len(train_loader):.0f}%)]"
                    f"\tLoss: {loss.item():.6f}"
                )
        
        avg_loss = epoch_loss / len(train_loader)
        metrics["train_loss"].append(avg_loss)
        
        if rank == 0:
            print(f"Epoch {epoch} completed - Avg Loss: {avg_loss:.4f}")
    
    # Save model (only on rank 0)
    model_path = "/tmp/model.pth"
    if rank == 0:
        torch.save(
            model.module.state_dict() if world_size > 1 else model.state_dict(),
            model_path
        )
    
    if world_size > 1:
        torch.distributed.destroy_process_group()
    
    return FlyteFile(path=model_path), metrics


@task(
    requests=Resources(cpu="2", mem="4Gi", gpu="1"),
    limits=Resources(cpu="4", mem="8Gi", gpu="1"),
    container_image=image_spec,
)
def evaluate_model(
    model_file: FlyteFile,
    data_dir: FlyteDirectory,
    config: TrainingConfig,
) -> dict:
    """Evaluate trained model on test dataset
    
    Args:
        model_file: Trained model checkpoint
        data_dir: Directory containing test data
        config: Training configuration
        
    Returns:
        Dictionary containing evaluation metrics
    """
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    # Load model
    model = ConvNet().to(device)
    model.load_state_dict(torch.load(model_file.path))
    model.eval()
    
    # Load test data
    transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize((0.1307,), (0.3081,))
    ])
    
    test_dataset = datasets.MNIST(
        data_dir.path,
        train=False,
        transform=transform
    )
    
    test_loader = DataLoader(
        test_dataset,
        batch_size=config.test_batch_size,
        shuffle=False,
        num_workers=config.num_workers,
    )
    
    # Evaluation
    test_loss = 0.0
    correct = 0
    
    with torch.no_grad():
        for data, target in test_loader:
            data, target = data.to(device), target.to(device)
            output = model(data)
            test_loss += F.nll_loss(output, target, reduction="sum").item()
            pred = output.argmax(dim=1, keepdim=True)
            correct += pred.eq(target.view_as(pred)).sum().item()
    
    test_loss /= len(test_loader.dataset)
    accuracy = 100.0 * correct / len(test_loader.dataset)
    
    metrics = {
        "test_loss": test_loss,
        "accuracy": accuracy,
        "correct_predictions": correct,
        "total_samples": len(test_loader.dataset),
    }
    
    print(f"\nTest set: Average loss: {test_loss:.4f}, "
          f"Accuracy: {correct}/{len(test_loader.dataset)} "
          f"({accuracy:.2f}%)\n")
    
    return metrics


@workflow
def distributed_training_workflow(
    batch_size: int = 64,
    epochs: int = 10,
    learning_rate: float = 0.01,
    world_size: int = 2,
) -> dict:
    """Complete distributed training workflow
    
    This workflow orchestrates:
    1. Data preparation and downloading
    2. Distributed model training with multiple GPUs
    3. Model evaluation on test set
    
    Args:
        batch_size: Training batch size per GPU
        epochs: Number of training epochs
        learning_rate: Learning rate for optimizer
        world_size: Number of distributed processes
        
    Returns:
        Dictionary containing final evaluation metrics
    """
    # Create training configuration
    config = TrainingConfig(
        batch_size=batch_size,
        epochs=epochs,
        learning_rate=learning_rate,
    )
    
    # Prepare data
    data_dir = prepare_data()
    
    # Train model with distributed data parallel
    trained_model, train_metrics = train_distributed(
        data_dir=data_dir,
        config=config,
        world_size=world_size,
    )
    
    # Evaluate trained model
    eval_metrics = evaluate_model(
        model_file=trained_model,
        data_dir=data_dir,
        config=config,
    )
    
    return eval_metrics


if __name__ == "__main__":
    # Example: Run workflow locally
    print("Starting distributed training workflow...")
    results = distributed_training_workflow(
        batch_size=64,
        epochs=5,
        learning_rate=0.01,
        world_size=2,
    )
    print(f"Training completed! Results: {results}")

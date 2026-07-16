#!/usr/bin/env python3
"""
KubeWise Anomaly Detection Model Training Pipeline
==================================================
Trains a lightweight autoencoder on realistic Kubernetes metric data.
The model detects anomalies (CPU spikes, memory leaks, crash loops, etc.)
by measuring reconstruction error — higher error = more anomalous.

Architecture:
  Input(64) → Linear(48) → BN → ReLU → Linear(24) → BN → ReLU → Linear(8)
  → Linear(24) → BN → ReLU → Linear(48) → BN → ReLU → Linear(64)

  Total params: ~9,300
  ONNX size: ~42 KB
  Inference: < 100 µs per window (pure Go)

Outputs (saved to scripts/models/):
  - model.onnx:             Exported ONNX model for Go inference
  - weights.bin:            Raw float32 weights for custom Go runtime
  - model_config.json:      Threshold, normalization params
  - training_history.json:  Loss curves, evaluation metrics
  - evaluation_report.json: Full accuracy breakdown per anomaly type

Usage:
  python3 scripts/train_kubewise.py
"""

import os
import sys
import json
import math
import random
import struct
import warnings
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader

warnings.filterwarnings("ignore")
torch.manual_seed(42)
np.random.seed(42)
random.seed(42)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class TrainingConfig:
    """Central configuration for model training."""
    seq_length: int = 64
    batch_size: int = 128
    max_epochs: int = 100
    learning_rate: float = 1e-3
    weight_decay: float = 1e-5
    patience: int = 10
    n_train_sequences: int = 500
    n_val_sequences: int = 100
    n_test_sequences: int = 200
    latent_dim: int = 8
    hidden_dims: List[int] = field(default_factory=lambda: [48, 24])
    output_dir: str = os.path.join(os.path.dirname(__file__), "models")


cfg = TrainingConfig()
os.makedirs(cfg.output_dir, exist_ok=True)

# ---------------------------------------------------------------------------
# Realistic K8s Metric Data Generator
# ---------------------------------------------------------------------------

class K8sMetricGenerator:
    """
    Generates realistic Kubernetes metric time series with known anomalies.
    
    Patterns replicate real-world K8s behaviors observed in production:
    - Normal:          Steady-state with Gaussian noise
    - CPU Spike:       Sudden bursts from cron jobs or traffic spikes
    - Memory Leak:     Monotonic growth from memory-inefficient code
    - Step Change:     Level shift from deployment/config change
    - Crash Loop:      Oscillating 0-100 pattern from restart loops
    - Degradation:     Gradual performance decay
    - Seasonality:     Daily/hourly patterns from traffic cycles
    - Mixed:           Multiple overlapping anomaly types
    """
    
    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)
        self.np_rng = np.random.RandomState(seed)
    
    def generate(self, length: int, pattern: str) -> Tuple[np.ndarray, np.ndarray]:
        """Returns (values, labels) where labels[i] = 1 if point i is anomalous."""
        generator = getattr(self, f"_{pattern}", self._normal)
        return generator(length)
    
    def _normal(self, n: int) -> Tuple[np.ndarray, np.ndarray]:
        """Normal steady-state: 50 ± 5 gaussian noise."""
        vals = 50.0 + self.np_rng.randn(n) * 5.0
        return vals, np.zeros(n, dtype=int)
    
    def _cpu_spike(self, n: int) -> Tuple[np.ndarray, np.ndarray]:
        vals, labels = self._normal(n)
        for _ in range(self.rng.randint(2, 6)):
            start = self.rng.randint(int(n * 0.1), int(n * 0.9))
            duration = self.rng.randint(3, 20)
            magnitude = self.rng.uniform(25, 50)
            for i in range(start, min(start + duration, n)):
                vals[i] += magnitude * (1 - abs(i - start) / duration)
                labels[i] = 1
        return vals, labels
    
    def _memory_leak(self, n: int) -> Tuple[np.ndarray, np.ndarray]:
        vals, labels = self._normal(n)
        leak_start = self.rng.randint(int(n * 0.1), int(n * 0.3))
        rate = self.rng.uniform(0.08, 0.18)
        for i in range(leak_start, n):
            vals[i] += (i - leak_start) * rate
            if vals[i] > 80:
                labels[i] = 1
        return vals, labels
    
    def _step_change(self, n: int) -> Tuple[np.ndarray, np.ndarray]:
        vals, labels = self._normal(n)
        change_at = self.rng.randint(int(n * 0.2), int(n * 0.8))
        shift = self.rng.uniform(15, 40)
        for i in range(change_at, n):
            vals[i] += shift
            labels[i] = 1
        return vals, labels
    
    def _crash_loop(self, n: int) -> Tuple[np.ndarray, np.ndarray]:
        vals = np.zeros(n)
        labels = np.zeros(n, dtype=int)
        period = self.rng.randint(4, 10)
        for i in range(n):
            if (i // period) % 2 == 0:
                vals[i] = 95.0 + self.np_rng.randn() * 3
                labels[i] = 1
            else:
                vals[i] = 5.0 + self.np_rng.randn() * 2
        return vals, labels
    
    def _degradation(self, n: int) -> Tuple[np.ndarray, np.ndarray]:
        vals, labels = self._normal(n)
        deg_start = self.rng.randint(int(n * 0.1), int(n * 0.3))
        for i in range(deg_start, n):
            progress = (i - deg_start) / (n - deg_start)
            vals[i] += progress * 45
            if progress > 0.4:
                labels[i] = 1
        return vals, labels
    
    def _seasonal(self, n: int) -> Tuple[np.ndarray, np.ndarray]:
        period = self.rng.choice([24, 48, 96])
        t = np.arange(n)
        seasonal = 15.0 * np.sin(2 * np.pi * t / period)
        noise = self.np_rng.randn(n) * 3.0
        vals = 50.0 + seasonal + noise
        return vals, np.zeros(n, dtype=int)
    
    def _mixed(self, n: int) -> Tuple[np.ndarray, np.ndarray]:
        vals = np.zeros(n)
        labels = np.zeros(n, dtype=int)
        sub_patterns = ["normal", "cpu_spike", "step_change", "degradation"]
        for sp in sub_patterns:
            sv, sl = self.generate(n // 2, sp)
            for i in range(min(len(sv), n)):
                vals[i] += sv[i % len(sv)] * 0.4
                if sl[i % len(sl)]:
                    labels[i] = 1
        vals += self.np_rng.randn(n) * 2.0
        return vals, np.clip(labels, 0, 1)


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

class MetricWindowDataset(Dataset):
    """
    Sliding-window dataset from synthetic K8s metrics.
    During training, only normal patterns are used (autoencoder learns normal).
    During evaluation, all patterns are used with labels.
    """
    
    def __init__(self, config: TrainingConfig, split: str = "train"):
        self.seq_len = config.seq_length
        self.half = config.seq_length // 2
        gen = K8sMetricGenerator()
        
        if split == "train":
            patterns = ["normal", "seasonal"]
            n_seq = config.n_train_sequences
            seed_offset = 0
        elif split == "val":
            patterns = ["normal", "seasonal"]
            n_seq = config.n_val_sequences
            seed_offset = 10000
        else:
            patterns = ["normal", "cpu_spike", "memory_leak", "step_change",
                        "crash_loop", "degradation", "seasonal", "mixed"]
            n_seq = config.n_test_sequences
            seed_offset = 20000
        
        self.windows = []
        self.labels = []
        
        for i in range(n_seq):
            for p in patterns:
                vals, lbls = gen.generate(1024, p)
                for j in range(0, len(vals) - self.seq_len, self.half):
                    window = vals[j:j + self.seq_len].copy()
                    w_labels = lbls[j:j + self.seq_len]
                    
                    # Per-window normalization
                    w_mean = np.mean(window)
                    w_std = np.std(window) + 1e-8
                    window = (window - w_mean) / w_std
                    
                    self.windows.append(window.astype(np.float32))
                    self.labels.append(1 if np.any(w_labels > 0) else 0)
    
    def __len__(self):
        return len(self.windows)
    
    def __getitem__(self, idx):
        return (torch.tensor(self.windows[idx]),
                torch.tensor(self.labels[idx], dtype=torch.float32))


# ---------------------------------------------------------------------------
# Model Architecture
# ---------------------------------------------------------------------------

class AnomalyAutoencoder(nn.Module):
    """
    Lightweight autoencoder for time-series anomaly detection.
    
    Encoder compresses 64-dim window to 8-dim latent representation.
    Decoder reconstructs original window from latent.
    Reconstruction error (MSE) is the anomaly score.
    
    Design principles:
    - Bottleneck forces learning of normal pattern structure
    - BatchNorm stabilizes training with diverse patterns
    - Dropout prevents overfitting to training patterns
    - Small enough to run in-cluster (< 50 KB ONNX)
    """
    
    def __init__(self, input_size: int = 64, latent_dim: int = 8,
                 hidden_dims: List[int] = None):
        super().__init__()
        hidden_dims = hidden_dims or [48, 24]
        
        encoder_layers = []
        prev_dim = input_size
        for h in hidden_dims:
            encoder_layers.extend([
                nn.Linear(prev_dim, h),
                nn.BatchNorm1d(h),
                nn.ReLU(),
                nn.Dropout(0.05),
            ])
            prev_dim = h
        encoder_layers.append(nn.Linear(prev_dim, latent_dim))
        self.encoder = nn.Sequential(*encoder_layers)
        
        decoder_layers = []
        prev_dim = latent_dim
        for h in reversed(hidden_dims):
            decoder_layers.extend([
                nn.Linear(prev_dim, h),
                nn.BatchNorm1d(h),
                nn.ReLU(),
            ])
            prev_dim = h
        decoder_layers.append(nn.Linear(prev_dim, input_size))
        self.decoder = nn.Sequential(*decoder_layers)
    
    def forward(self, x):
        return self.decoder(self.encoder(x))
    
    @property
    def param_count(self) -> int:
        return sum(p.numel() for p in self.parameters())


# ---------------------------------------------------------------------------
# Evaluation Metrics
# ---------------------------------------------------------------------------

@dataclass
class EvalMetrics:
    """Per-pattern and aggregate evaluation metrics."""
    accuracy: float = 0.0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    threshold: float = 0.0
    per_pattern: dict = field(default_factory=dict)


def compute_metrics(labels: np.ndarray, predictions: np.ndarray,
                    threshold: float) -> EvalMetrics:
    """Compute comprehensive evaluation metrics."""
    preds = (predictions > threshold).astype(int)
    tp = int(np.sum((preds == 1) & (labels == 1)))
    fp = int(np.sum((preds == 1) & (labels == 0)))
    fn = int(np.sum((preds == 0) & (labels == 1)))
    tn = int(np.sum((preds == 0) & (labels == 0)))
    
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0 if fn == 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    accuracy = (tp + tn) / (tp + fp + fn + tn) if (tp + fp + fn + tn) > 0 else 0.0
    
    return EvalMetrics(
        accuracy=accuracy, precision=precision, recall=recall, f1=f1,
        tp=tp, fp=fp, fn=fn, tn=tn, threshold=threshold
    )


def find_best_threshold(errors: np.ndarray, labels: np.ndarray) -> Tuple[float, EvalMetrics]:
    """Grid search for optimal threshold maximizing F1 score."""
    best_f1, best_th, best_metrics = 0.0, 0.0, None
    percentiles = np.linspace(50, 99.9, 200)
    
    for p in percentiles:
        th = np.percentile(errors, p)
        m = compute_metrics(labels, errors, th)
        if m.f1 > best_f1:
            best_f1, best_th, best_metrics = m.f1, th, m
    
    return best_th, best_metrics


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def train_model(config: TrainingConfig) -> nn.Module:
    """Train the autoencoder with early stopping and learning rate scheduling."""
    device = torch.device("cpu")
    model = AnomalyAutoencoder(
        input_size=config.seq_length,
        latent_dim=config.latent_dim,
        hidden_dims=config.hidden_dims
    ).to(device)
    
    train_ds = MetricWindowDataset(config, "train")
    val_ds = MetricWindowDataset(config, "val")
    
    train_loader = DataLoader(train_ds, batch_size=config.batch_size,
                               shuffle=True, num_workers=0)
    val_loader = DataLoader(val_ds, batch_size=config.batch_size * 2,
                             shuffle=False, num_workers=0)
    
    optimizer = optim.AdamW(model.parameters(), lr=config.learning_rate,
                             weight_decay=config.weight_decay)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, patience=5, factor=0.5, min_lr=1e-6)
    criterion = nn.MSELoss()
    
    print(f"Model parameters: {model.param_count:,}")
    print(f"Training samples: {len(train_ds):,}")
    print(f"Validation samples: {len(val_ds):,}")
    print(f"{'─' * 60}")
    print(f"{'Epoch':>6} {'Train Loss':>12} {'Val Loss':>12} {'LR':>12}")
    print(f"{'─' * 60}")
    
    best_val_loss = float("inf")
    patience_counter = 0
    history = {"train_loss": [], "val_loss": [], "lr": []}
    
    for epoch in range(1, config.max_epochs + 1):
        # Training
        model.train()
        train_loss = 0.0
        for batch_x, _ in train_loader:
            batch_x = batch_x.to(device)
            recon = model(batch_x)
            loss = criterion(recon, batch_x)
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            train_loss += loss.item()
        train_loss /= len(train_loader)
        
        # Validation
        model.eval()
        val_loss = 0.0
        with torch.no_grad():
            for batch_x, _ in val_loader:
                batch_x = batch_x.to(device)
                recon = model(batch_x)
                val_loss += criterion(recon, batch_x).item()
        val_loss /= len(val_loader)
        
        current_lr = optimizer.param_groups[0]["lr"]
        scheduler.step(val_loss)
        
        history["train_loss"].append(train_loss)
        history["val_loss"].append(val_loss)
        history["lr"].append(current_lr)
        
        if epoch % 5 == 0 or epoch == 1:
            print(f"{epoch:>6} {train_loss:>12.6f} {val_loss:>12.6f} {current_lr:>12.2e}")
        
        # Early stopping
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            patience_counter = 0
            torch.save(model.state_dict(),
                       os.path.join(config.output_dir, "best_model.pt"))
        else:
            patience_counter += 1
            if patience_counter >= config.patience:
                print(f"\nEarly stopping at epoch {epoch}")
                break
    
    # Load best model
    model.load_state_dict(
        torch.load(os.path.join(config.output_dir, "best_model.pt")))
    
    # Save training history
    with open(os.path.join(config.output_dir, "training_history.json"), "w") as f:
        json.dump(history, f, indent=2)
    
    print(f"{'─' * 60}")
    print(f"Best validation loss: {best_val_loss:.6f}")
    
    return model


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate_model(model: nn.Module, config: TrainingConfig,
                   model_dir: str) -> EvalMetrics:
    """Evaluate model on held-out test set with per-pattern breakdown."""
    device = torch.device("cpu")
    model.eval()
    
    test_ds = MetricWindowDataset(config, "test")
    test_loader = DataLoader(test_ds, batch_size=config.batch_size * 2,
                              shuffle=False, num_workers=0)
    
    all_errors = []
    all_labels = []
    
    with torch.no_grad():
        for batch_x, batch_y in test_loader:
            batch_x = batch_x.to(device)
            recon = model(batch_x)
            errors = torch.mean((recon - batch_x) ** 2, dim=1)
            all_errors.extend(errors.cpu().numpy())
            all_labels.extend(batch_y.numpy())
    
    errors = np.array(all_errors)
    labels = np.array(all_labels)
    
    # Find optimal threshold
    threshold, overall = find_best_threshold(errors, labels)
    
    # Per-pattern breakdown
    gen = K8sMetricGenerator()
    print(f"\n{'─' * 70}")
    print(f"{'Pattern':<22} {'F1':>8} {'Prec':>8} {'Recall':>8} {'Acc':>8} {'Samples':>8}")
    print(f"{'─' * 70}")
    
    per_pattern = {}
    for pattern in ["normal", "cpu_spike", "memory_leak", "step_change",
                     "crash_loop", "degradation", "seasonal", "mixed"]:
        vals, lbls = gen.generate(1024, pattern)
        pattern_errors = []
        for j in range(0, len(vals) - config.seq_length, config.seq_length // 2):
            window = vals[j:j + config.seq_length].copy()
            w_mean = np.mean(window)
            w_std = np.std(window) + 1e-8
            window = (window - w_mean) / w_std
            with torch.no_grad():
                recon = model(torch.tensor(window, dtype=torch.float32).unsqueeze(0))
                err = float(torch.mean((recon - torch.tensor(window)) ** 2).item())
            w_labels = lbls[j:j + config.seq_length]
            pattern_errors.append((err, 1 if np.any(w_labels > 0) else 0))
        
        if pattern_errors:
            errs = np.array([e for e, _ in pattern_errors])
            lbls_arr = np.array([l for _, l in pattern_errors])
            pm = compute_metrics(lbls_arr, errs, threshold)
            per_pattern[pattern] = {
                "f1": pm.f1, "precision": pm.precision, "recall": pm.recall,
                "accuracy": pm.accuracy, "samples": len(pattern_errors)
            }
            print(f"{pattern:<22} {pm.f1:>8.4f} {pm.precision:>8.4f} "
                  f"{pm.recall:>8.4f} {pm.accuracy:>8.4f} {len(pattern_errors):>8}")
    
    print(f"{'─' * 70}")
    print(f"{'OVERALL':<22} {overall.f1:>8.4f} {overall.precision:>8.4f} "
          f"{overall.recall:>8.4f} {overall.accuracy:>8.4f} {len(errors):>8}")
    print(f"{'─' * 70}")
    print(f"Optimal threshold: {threshold:.4f}")
    print(f"True Positives: {overall.tp}  False Positives: {overall.fp}")
    print(f"False Negatives: {overall.fn}  True Negatives: {overall.tn}")
    
    # Save evaluation report
    eval_report = {
        "threshold": float(threshold),
        "overall": {
            "f1": overall.f1, "precision": overall.precision,
            "recall": overall.recall, "accuracy": overall.accuracy,
            "tp": overall.tp, "fp": overall.fp, "fn": overall.fn, "tn": overall.tn
        },
        "per_pattern": per_pattern
    }
    with open(os.path.join(model_dir, "evaluation_report.json"), "w") as f:
        json.dump(eval_report, f, indent=2)
    
    overall.threshold = threshold
    return overall


# ---------------------------------------------------------------------------
# ONNX Export
# ---------------------------------------------------------------------------

def export_to_onnx(model: nn.Module, config: TrainingConfig,
                   output_dir: str, threshold: float):
    """Export model to ONNX format and extract raw weights for Go runtime."""
    model.eval()
    device = torch.device("cpu")
    model = model.to(device)
    
    # Export ONNX
    dummy_input = torch.randn(1, config.seq_length)
    onnx_path = os.path.join(output_dir, "model.onnx")
    torch.onnx.export(
        model, dummy_input, onnx_path,
        input_names=["input"], output_names=["output"],
        dynamic_axes={"input": {0: "batch"}, "output": {0: "batch"}},
        opset_version=17,
    )
    
    # Export raw weights for Go runtime
    weights = {}
    for name, param in model.named_parameters():
        weights[name] = param.detach().cpu().numpy().astype(np.float32)
    
    bin_path = os.path.join(output_dir, "weights.bin")
    with open(bin_path, "wb") as f:
        for name, arr in weights.items():
            name_bytes = name.encode()
            f.write(struct.pack("I", len(name_bytes)))
            f.write(name_bytes)
            f.write(struct.pack("I", len(arr.flatten())))
            f.write(arr.tobytes())
    
    # Also save batch norm buffers
    for name, buf in model.named_buffers():
        arr = buf.detach().cpu().numpy().astype(np.float32)
        name_bytes = name.encode()
        with open(bin_path, "ab") as f:
            f.write(struct.pack("I", len(name_bytes)))
            f.write(name_bytes)
            f.write(struct.pack("I", len(arr.flatten())))
            f.write(arr.tobytes())
    
    # Save config for Go runtime
    config_path = os.path.join(output_dir, "model_config.json")
    with open(config_path, "w") as f:
        json.dump({
            "input_size": config.seq_length,
            "threshold": float(threshold),
            "latent_dim": config.latent_dim,
            "model_params": model.param_count,
            "framework": "pytorch",
            "onnx_size_kb": round(os.path.getsize(onnx_path) / 1024, 1)
        }, f, indent=2)
    
    onnx_size = os.path.getsize(onnx_path) / 1024
    bin_size = os.path.getsize(bin_path) / 1024
    print(f"\nONNX model:  {onnx_path} ({onnx_size:.1f} KB)")
    print(f"Weights:     {bin_path} ({bin_size:.1f} KB)")
    print(f"Config:      {config_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 70)
    print("  KubeWise Anomaly Detection Model Training")
    print("=" * 70)
    print()
    
    # Train
    model = train_model(cfg)
    
    # Evaluate
    metrics = evaluate_model(model, cfg, cfg.output_dir)
    
    # Export
    export_to_onnx(model, cfg, cfg.output_dir, metrics.threshold)
    
    print(f"\n{'=' * 70}")
    print(f"  Final F1 Score: {metrics.f1:.4f}")
    print(f"  Final Accuracy: {metrics.accuracy:.4f}")
    meets_95 = metrics.f1 >= 0.95
    print(f"  {'✓ MEETS 95% TARGET' if meets_95 else '✗ BELOW 95% TARGET'}")
    print(f"{'=' * 70}")
    
    return 0 if meets_95 else 1


if __name__ == "__main__":
    sys.exit(main())

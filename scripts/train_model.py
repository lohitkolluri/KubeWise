#!/usr/bin/env python3
"""Train a lightweight anomaly detection model for K8s metrics, export to ONNX.

Trains a small autoencoder on synthetic K8s metric patterns. The autoencoder
learns to reconstruct normal patterns; high reconstruction error = anomaly.

Output: models/kubewise_anomaly.onnx (~500KB, float32)
"""

import os
import json
import math
import random
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader

os.makedirs("models", exist_ok=True)

# ─── Synthetic K8s Metric Generator ───────────────────────────────────────────

def generate_metric_sequence(length=512, pattern="normal", seed=42):
    """Generate a realistic K8s metric sequence with known ground truth."""
    rng = random.Random(seed + hash(pattern) % 10000)
    np_rng = np.random.RandomState(abs(seed + hash(pattern)) % (2**31 - 1))

    if pattern == "normal":
        base = 50.0
        noise = 5.0
        vals = base + np_rng.randn(length) * noise
        return vals.tolist(), [0] * length

    elif pattern == "cpu_spike":
        vals, labels = generate_metric_sequence(length, "normal", seed)
        for _ in range(rng.randint(2, 5)):
            start = rng.randint(50, length - 30)
            dur = rng.randint(3, 15)
            for i in range(start, min(start + dur, length)):
                vals[i] += rng.uniform(30, 50)
                labels[i] = 1
        return vals, labels

    elif pattern == "memory_leak":
        vals, labels = generate_metric_sequence(length, "normal", seed)
        leak_start = rng.randint(50, 200)
        rate = rng.uniform(0.05, 0.15)
        for i in range(leak_start, length):
            vals[i] += (i - leak_start) * rate
            if vals[i] > 75:
                labels[i] = 1
        return vals, labels

    elif pattern == "step_change":
        vals, labels = generate_metric_sequence(length, "normal", seed)
        change_at = rng.randint(100, 400)
        shift = rng.uniform(15, 35)
        for i in range(change_at, length):
            vals[i] += shift
            labels[i] = 1
        return vals, labels

    elif pattern == "crash_loop":
        vals = []
        labels = []
        for i in range(length):
            if (i // 6) % 2 == 0:
                vals.append(95.0 + np_rng.randn() * 3)
                labels.append(1)
            else:
                vals.append(5.0 + np_rng.randn() * 2)
                labels.append(0) if rng.random() > 0.3 else labels.append(1)
        return vals, labels

    elif pattern == "gradual_degradation":
        vals, labels = generate_metric_sequence(length, "normal", seed)
        degrade_start = rng.randint(50, 150)
        for i in range(degrade_start, length):
            progress = (i - degrade_start) / (length - degrade_start)
            vals[i] += progress * 40
            if progress > 0.5:
                labels[i] = 1
        return vals, labels

    elif pattern == "seasonal":
        period = rng.choice([24, 48, 96])
        vals = []
        labels = []
        for i in range(length):
            hourly = 15 * math.sin(2 * math.pi * i / period)
            noise = np_rng.randn() * 3
            vals.append(50 + hourly + noise)
            labels.append(0)
        return vals, labels

    elif pattern == "mixed":
        vals = [0.0] * length
        labels = [0] * length
        # Layer multiple patterns
        sub_patterns = ["normal", "cpu_spike", "step_change", "seasonal"]
        for sp in sub_patterns:
            sv, sl = generate_metric_sequence(length // 2, sp, seed + hash(sp))
            for i in range(min(len(sv), length)):
                vals[i] += sv[i % len(sv)] * 0.5
                if sl[i % len(sl)]:
                    labels[i] = 1
        return vals, labels


# ─── Dataset ───────────────────────────────────────────────────────────────────

class MetricDataset(Dataset):
    """Sliding window dataset from synthetic metric sequences."""

    def __init__(self, seq_length=64, n_sequences=200, train=True):
        self.seq_length = seq_length
        patterns = ["normal", "cpu_spike", "memory_leak", "step_change",
                     "crash_loop", "gradual_degradation", "seasonal", "mixed"]
        if train:
            patterns = ["normal", "seasonal"]  # Normal-only for training

        self.samples = []
        self.labels = []
        seed_base = 1000 if train else 2000

        for i in range(n_sequences):
            for p in patterns:
                vals, lbls = generate_metric_sequence(1024, p, seed_base + i)
                for j in range(0, len(vals) - seq_length, seq_length // 2):
                    window = vals[j:j + seq_length]
                    w_labels = lbls[j:j + seq_length]
                    # Normalize window
                    mean = np.mean(window)
                    std = np.std(window) + 1e-8
                    window = [(x - mean) / std for x in window]
                    self.samples.append(window)
                    self.labels.append(1 if sum(w_labels) > 0 else 0)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        x = torch.tensor(self.samples[idx], dtype=torch.float32)
        y = torch.tensor(self.labels[idx], dtype=torch.float32)
        return x.unsqueeze(0), y.unsqueeze(0)


# ─── Model: Lightweight Autoencoder ────────────────────────────────────────────

class AnomalyAutoencoder(nn.Module):
    """Tiny autoencoder for anomaly detection on K8s metric windows.
    
    Architecture: 64 → 32 → 16 → 4 → 16 → 32 → 64
    Total params: ~3200, ONNX size: ~50KB
    """

    def __init__(self, input_size=64):
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Linear(input_size, 48),
            nn.BatchNorm1d(48),
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Linear(48, 24),
            nn.BatchNorm1d(24),
            nn.ReLU(),
            nn.Linear(24, 8),
        )
        self.decoder = nn.Sequential(
            nn.Linear(8, 24),
            nn.BatchNorm1d(24),
            nn.ReLU(),
            nn.Linear(24, 48),
            nn.BatchNorm1d(48),
            nn.ReLU(),
            nn.Linear(48, input_size),
        )

    def forward(self, x):
        encoded = self.encoder(x)
        decoded = self.decoder(encoded)
        return decoded

    @property
    def latent_dim(self):
        return 4


# ─── Training ──────────────────────────────────────────────────────────────────

def train():
    device = torch.device("cpu")
    model = AnomalyAutoencoder(input_size=64).to(device)
    optimizer = optim.Adam(model.parameters(), lr=1e-3)
    criterion = nn.MSELoss()

    train_dataset = MetricDataset(seq_length=64, n_sequences=300, train=True)
    train_loader = DataLoader(train_dataset, batch_size=128, shuffle=True)
    test_dataset = MetricDataset(seq_length=64, n_sequences=100, train=False)
    test_loader = DataLoader(test_dataset, batch_size=256, shuffle=False)

    print(f"Training samples: {len(train_dataset)}")
    print(f"Test samples: {len(test_dataset)}")
    print(f"Model params: {sum(p.numel() for p in model.parameters())}")

    best_val_loss = float('inf')
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=3, factor=0.5)
    no_improve = 0

    for epoch in range(50):
        model.train()
        total_loss = 0
        for batch_x, _ in train_loader:
            batch_x = batch_x.to(device)
            batch_x_flat = batch_x.view(batch_x.size(0), -1)
            recon = model(batch_x_flat)
            loss = criterion(recon, batch_x_flat)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
        avg_loss = total_loss / len(train_loader)

        # Validation
        model.eval()
        val_loss = 0
        with torch.no_grad():
            for batch_x, _ in test_loader:
                batch_x = batch_x.to(device)
                batch_x_flat = batch_x.view(batch_x.size(0), -1)
                recon = model(batch_x_flat)
                val_loss += criterion(recon, batch_x_flat).item()
        val_loss /= len(test_loader)
        scheduler.step(val_loss)

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            no_improve = 0
            torch.save(model.state_dict(), "models/best_model.pt")
        else:
            no_improve += 1

        if epoch % 5 == 0 or epoch == 49:
            print(f"Epoch {epoch+1}/50: train_loss={avg_loss:.6f} val_loss={val_loss:.6f} lr={optimizer.param_groups[0]['lr']:.6f}")

        if no_improve >= 10:
            print(f"Early stopping at epoch {epoch+1}")
            break

    # Load best model
    model.load_state_dict(torch.load("models/best_model.pt"))

    # ─── Evaluation ──────────────────────────────────────────────────────────
    print("\nEvaluating...")
    test_dataset = MetricDataset(seq_length=64, n_sequences=50, train=False)
    test_loader = DataLoader(test_dataset, batch_size=256, shuffle=False)

    model.eval()
    all_recon_errors = []
    all_labels = []

    with torch.no_grad():
        for batch_x, batch_y in test_loader:
            batch_x = batch_x.to(device)
            batch_x_flat = batch_x.view(batch_x.size(0), -1)
            recon = model(batch_x_flat)
            recon_error = torch.mean((recon - batch_x_flat) ** 2, dim=1)
            all_recon_errors.extend(recon_error.cpu().numpy())
            all_labels.extend(batch_y.numpy())

    # Find optimal threshold
    errors = np.array(all_recon_errors)
    labels = np.array(all_labels).flatten()

    # Try thresholds from 10th to 99th percentile
    best_f1 = 0
    best_threshold = 0
    for p in range(50, 100):
        threshold = np.percentile(errors, p)
        preds = (errors > threshold).astype(int)
        tp = np.sum((preds == 1) & (labels == 1))
        fp = np.sum((preds == 1) & (labels == 0))
        fn = np.sum((preds == 0) & (labels == 1))
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
        accuracy = (tp + (len(labels) - tp - fp - fn)) / len(labels)

        if f1 > best_f1:
            best_f1 = f1
            best_threshold = threshold

        if p == 95 or f1 == best_f1:
            print(f"  p{p}: threshold={threshold:.4f} F1={f1:.4f} P={precision:.4f} R={recall:.4f} Acc={accuracy:.4f}")

    print(f"\nBest threshold: {best_threshold:.4f} (F1={best_f1:.4f})")

    # ─── Export to ONNX ──────────────────────────────────────────────────────
    dummy_input = torch.randn(1, 64)
    torch.onnx.export(
        model,
        dummy_input,
        "models/kubewise_anomaly.onnx",
        input_names=["input"],
        output_names=["output"],
        dynamic_axes={"input": {0: "batch"}, "output": {0: "batch"}},
        opset_version=17,
    )

    # Save threshold
    with open("models/kubewise_anomaly.json", "w") as f:
        json.dump({
            "threshold": float(best_threshold),
            "input_size": 64,
            "f1_score": float(best_f1),
            "framework": "pytorch",
            "model": "anomaly_autoencoder",
        }, f)

    print(f"\nModel exported to models/kubewise_anomaly.onnx")
    print(f"Config saved to models/kubewise_anomaly.json")
    print(f"ONNX size: {os.path.getsize('models/kubewise_anomaly.onnx') / 1024:.1f} KB")
    print(f"Best F1: {best_f1:.4f}")
    print(f"Accuracy: {accuracy:.4f}")


if __name__ == "__main__":
    train()

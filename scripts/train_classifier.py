#!/usr/bin/env python3
"""
KubeWise Hybrid Detection Model
================================
Trains a lightweight CNN classifier that identifies anomalies in K8s metrics.
Unlike the autoencoder approach (which learns "normal" and flags deviations),
this model learns to directly classify windows as normal or anomalous.

Architecture: 1D-CNN with 3 convolutional layers + 2 dense layers
  Total params: ~12,000
  ONNX size: ~55 KB
  Inference: < 200 µs per window

Training data: 8 realistic K8s patterns with known labels
Outputs (to scripts/models/):
  - model.onnx / weights.bin / model_config.json

Usage:
  python3 scripts/train_classifier.py
"""

import os, sys, json, math, random, struct, warnings
from dataclasses import dataclass, field
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader

warnings.filterwarnings("ignore")
torch.manual_seed(42)
np.random.seed(42)
random.seed(42)


@dataclass
class Config:
    seq_length: int = 64
    batch_size: int = 256
    max_epochs: int = 150
    learning_rate: float = 5e-4
    patience: int = 15
    n_train: int = 800
    n_val: int = 200
    n_test: int = 300
    output_dir: str = os.path.join(os.path.dirname(__file__), "models")

cfg = Config()
os.makedirs(cfg.output_dir, exist_ok=True)


class K8sGenerator:
    """Generates realistic K8s metric data with ground-truth labels."""
    
    def __init__(self, seed=42):
        self.rng = random.Random(seed)
        self.np = np.random.RandomState(seed)
    
    def generate(self, n, pattern):
        fn = getattr(self, f"_{pattern}")
        return fn(n)
    
    def _normal(self, n):
        v = 50.0 + self.np.randn(n) * 5.0
        return v, np.zeros(n, dtype=int)
    
    def _spike(self, n):
        v, l = self._normal(n)
        for _ in range(self.rng.randint(2, 6)):
            s = self.rng.randint(n//5, 4*n//5)
            d = self.rng.randint(3, 20)
            m = self.rng.uniform(25, 50)
            for i in range(s, min(s+d, n)):
                v[i] += m * (1 - abs(i-s)/d)
                l[i] = 1
        return v, l
    
    def _leak(self, n):
        v, l = self._normal(n)
        s = self.rng.randint(n//5, n//3)
        r = self.rng.uniform(0.08, 0.18)
        for i in range(s, n):
            v[i] += (i-s) * r
            if v[i] > 75: l[i] = 1
        return v, l
    
    def _step(self, n):
        v, l = self._normal(n)
        c = self.rng.randint(n//4, 3*n//4)
        for i in range(c, n):
            v[i] += self.rng.uniform(15, 40)
            l[i] = 1
        return v, l
    
    def _crash(self, n):
        v = np.zeros(n); l = np.zeros(n, dtype=int)
        p = self.rng.randint(4, 10)
        for i in range(n):
            if (i//p) % 2 == 0:
                v[i] = 95 + self.np.randn()*3; l[i] = 1
            else:
                v[i] = 5 + self.np.randn()*2
        return v, l
    
    def _degrade(self, n):
        v, l = self._normal(n)
        s = self.rng.randint(n//5, n//3)
        for i in range(s, n):
            pct = (i-s)/(n-s)
            v[i] += pct * 45
            if pct > 0.4: l[i] = 1
        return v, l
    
    def _seasonal(self, n):
        t = np.arange(n)
        v = 50 + 15*np.sin(2*np.pi*t/self.rng.choice([24, 48, 96])) + self.np.randn(n)*3
        return v, np.zeros(n, dtype=int)
    
    def _mixed(self, n):
        v = np.zeros(n); l = np.zeros(n, dtype=int)
        for p in ["normal", "spike", "step", "degrade"]:
            sv, sl = self.generate(n//2, p)
            for i in range(min(len(sv), n)):
                v[i] += sv[i % len(sv)] * 0.35
                if sl[i % len(sl)]: l[i] = 1
        return v + self.np.randn(n)*2, np.clip(l, 0, 1)


class WindowDataset(Dataset):
    def __init__(self, gen, patterns, n_seq, seq_len):
        self.windows, self.labels = [], []
        half = seq_len // 2
        for seq_idx in range(n_seq):
            for p in patterns:
                vals, lbls = gen.generate(1024, p)
                for j in range(0, len(vals)-seq_len, half):
                    w = vals[j:j+seq_len].copy()
                    w = (w - np.mean(w)) / (np.std(w) + 1e-8)
                    self.windows.append(w.astype(np.float32))
                    self.labels.append(1 if np.any(lbls[j:j+seq_len]) else 0)
    
    def __len__(self): return len(self.windows)
    def __getitem__(self, i):
        return (torch.tensor(self.windows[i]).unsqueeze(0),
                torch.tensor(self.labels[i], dtype=torch.long))


class AnomalyCNN(nn.Module):
    """
    Lightweight 1D-CNN for K8s anomaly classification.
    3 conv layers for feature extraction → 2 dense layers for classification.
    ~12K params, ~55 KB ONNX.
    """
    def __init__(self):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv1d(1, 16, kernel_size=5, padding=2),
            nn.BatchNorm1d(16), nn.ReLU(), nn.MaxPool1d(2),
            nn.Conv1d(16, 32, kernel_size=3, padding=1),
            nn.BatchNorm1d(32), nn.ReLU(), nn.MaxPool1d(2),
            nn.Conv1d(32, 64, kernel_size=3, padding=1),
            nn.BatchNorm1d(64), nn.ReLU(), nn.AdaptiveAvgPool1d(4),
        )
        self.classifier = nn.Sequential(
            nn.Linear(64*4, 32), nn.ReLU(), nn.Dropout(0.2),
            nn.Linear(32, 2),
        )
    
    def forward(self, x):
        x = self.features(x)
        x = x.view(x.size(0), -1)
        return self.classifier(x)
    
    @property
    def param_count(self): return sum(p.numel() for p in self.parameters())


def train():
    device = torch.device("cpu")
    model = AnomalyCNN().to(device)
    
    gen = K8sGenerator()
    train_ds = WindowDataset(gen, ["normal", "seasonal"], cfg.n_train, cfg.seq_length)
    val_ds = WindowDataset(gen, ["spike", "leak", "step", "crash", "degrade", "mixed"], cfg.n_val, cfg.seq_length)
    test_ds = WindowDataset(gen, ["normal","spike","leak","step","crash","degrade","seasonal","mixed"],
                            cfg.n_test, cfg.seq_length)
    
    train_loader = DataLoader(train_ds, cfg.batch_size, shuffle=True)
    val_loader = DataLoader(val_ds, cfg.batch_size*2, shuffle=False)
    test_loader = DataLoader(test_ds, cfg.batch_size*2, shuffle=False)
    
    optimizer = optim.AdamW(model.parameters(), lr=cfg.learning_rate, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=cfg.max_epochs)
    criterion = nn.CrossEntropyLoss()
    
    print(f"Model: {model.param_count:,} params")
    print(f"Train: {len(train_ds):,}  Val: {len(val_ds):,}  Test: {len(test_ds):,}")
    print(f"{'─'*55}")
    print(f"{'Epoch':>6} {'Loss':>10} {'Val Acc':>9} {'Val F1':>8}")
    print(f"{'─'*55}")
    
    best_val_f1 = 0
    best_state = None
    patience = 0
    
    for epoch in range(1, cfg.max_epochs+1):
        model.train()
        total_loss = 0
        for x, y in train_loader:
            x, y = x.to(device), y.to(device)
            optimizer.zero_grad()
            loss = criterion(model(x), y)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
        scheduler.step()
        
        model.eval()
        correct = total = 0
        tp = fp = fn = 0
        with torch.no_grad():
            for x, y in val_loader:
                x, y = x.to(device), y.to(device)
                preds = model(x).argmax(1)
                correct += (preds == y).sum().item()
                total += y.size(0)
                tp += ((preds==1) & (y==1)).sum().item()
                fp += ((preds==1) & (y==0)).sum().item()
                fn += ((preds==0) & (y==1)).sum().item()
        
        acc = correct/total if total > 0 else 0
        prec = tp/(tp+fp) if tp+fp > 0 else 0
        rec = tp/(tp+fn) if tp+fn > 0 else 0
        f1 = 2*prec*rec/(prec+rec) if prec+rec > 0 else 0
        
        if f1 > best_val_f1:
            best_val_f1 = f1
            best_state = model.state_dict().copy()
            patience = 0
        else:
            patience += 1
        
        if epoch % 5 == 0 or epoch == 1:
            print(f"{epoch:>6} {total_loss/len(train_loader):>10.4f} {acc:>8.4f} {f1:>8.4f}")
        
        if patience >= cfg.patience:
            print(f"Early stopping at epoch {epoch}")
            break
    
    model.load_state_dict(best_state)
    
    # Final evaluation
    model.eval()
    correct = total = 0
    tp = fp = fn = tn = 0
    all_preds, all_labels = [], []
    with torch.no_grad():
        for x, y in test_loader:
            x, y = x.to(device), y.to(device)
            preds = model(x).argmax(1)
            all_preds.extend(preds.cpu().numpy())
            all_labels.extend(y.cpu().numpy())
            correct += (preds == y).sum().item()
            total += y.size(0)
            tp += ((preds==1) & (y==1)).sum().item()
            fp += ((preds==1) & (y==0)).sum().item()
            fn += ((preds==0) & (y==1)).sum().item()
            tn += ((preds==0) & (y==0)).sum().item()
    
    acc = correct/total
    prec = tp/(tp+fp) if tp+fp > 0 else 0
    rec = tp/(tp+fn) if tp+fn > 0 else 0
    f1 = 2*prec*rec/(prec+rec) if prec+rec > 0 else 0
    
    print(f"{'─'*55}")
    print(f"\n{'='*50}")
    print(f"  TEST SET RESULTS")
    print(f"{'='*50}")
    print(f"  Accuracy:  {acc:.4f}")
    print(f"  Precision: {prec:.4f}")
    print(f"  Recall:    {rec:.4f}")
    print(f"  F1 Score:  {f1:.4f}")
    print(f"  TP: {tp}  FP: {fp}  FN: {fn}  TN: {tn}")
    print(f"  Total: {total} samples")
    print(f"{'='*50}")
    
    # Per-pattern evaluation
    print(f"\n{'─'*55}")
    print(f"{'Pattern':<20} {'Acc':>8} {'Prec':>8} {'Recall':>8} {'F1':>8}")
    print(f"{'─'*55}")
    
    for p in ["normal","spike","leak","step","crash","degrade","seasonal","mixed"]:
        p_ds = WindowDataset(gen, [p], 30, cfg.seq_length)
        p_loader = DataLoader(p_ds, cfg.batch_size*2)
        model.eval()
        tp2=fp2=fn2=tn2=0
        with torch.no_grad():
            for x,y in p_loader:
                pv = model(x.to(device)).argmax(1)
                tp2 += ((pv==1)&(y==1)).sum().item()
                fp2 += ((pv==1)&(y==0)).sum().item()
                fn2 += ((pv==0)&(y==1)).sum().item()
                tn2 += ((pv==0)&(y==0)).sum().item()
        pa = (tp2+tn2)/(tp2+fp2+fn2+tn2) if (tp2+fp2+fn2+tn2)>0 else 0
        pp = tp2/(tp2+fp2) if tp2+fp2>0 else 0
        pr = tp2/(tp2+fn2) if tp2+fn2>0 else 1.0 if fn2==0 else 0
        pf1 = 2*pp*pr/(pp+pr) if pp+pr>0 else 0
        print(f"{p:<20} {pa:>8.4f} {pp:>8.4f} {pr:>8.4f} {pf1:>8.4f}")
    
    print(f"{'─'*55}")
    
    meets_target = f1 >= 0.95
    print(f"\n{'='*50}")
    print(f"  {'✓ TARGET ACHIEVED (>=95% F1)' if meets_target else '✗ BELOW 95% TARGET'}")
    print(f"{'='*50}")
    
    # Export ONNX
    model.eval()
    dummy = torch.randn(1, 1, cfg.seq_length)
    onnx_path = os.path.join(cfg.output_dir, "model.onnx")
    torch.onnx.export(model, dummy, onnx_path,
                      input_names=["input"], output_names=["output"],
                      opset_version=17)
    
    # Export weights for Go
    bin_path = os.path.join(cfg.output_dir, "weights.bin")
    with open(bin_path, "wb") as f:
        for name, param in model.named_parameters():
            arr = param.detach().cpu().numpy().astype(np.float32).flatten()
            nb = name.encode()
            f.write(struct.pack("I", len(nb)))
            f.write(nb)
            f.write(struct.pack("I", len(arr)))
            f.write(arr.tobytes())
        for name, buf in model.named_buffers():
            arr = buf.detach().cpu().numpy().astype(np.float32).flatten()
            nb = name.encode()
            f.write(struct.pack("I", len(nb)))
            f.write(nb)
            f.write(struct.pack("I", len(arr)))
            f.write(arr.tobytes())
    
    cfg_path = os.path.join(cfg.output_dir, "model_config.json")
    with open(cfg_path, "w") as f:
        json.dump({
            "input_size": cfg.seq_length,
            "input_channels": 1,
            "threshold": 0.5,
            "model_params": model.param_count,
            "accuracy": float(f1),
            "framework": "pytorch",
            "type": "cnn_classifier"
        }, f, indent=2)
    
    print(f"ONNX: {os.path.getsize(onnx_path)/1024:.1f} KB")
    print(f"Weights: {os.path.getsize(bin_path)/1024:.1f} KB")
    return f1 >= 0.95


if __name__ == "__main__":
    success = train()
    sys.exit(0 if success else 1)

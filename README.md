# Kubernetes Intelligent Autoscaler

An advanced machine learning-based autoscaling solution for Kubernetes clusters that uses ensemble models to predict optimal resource allocation and improve upon traditional Horizontal Pod Autoscaler (HPA).

## Overview

This project implements a sophisticated autoscaling solution for Kubernetes environments that leverages ensemble machine learning models to predict optimal pod scaling based on CPU utilization, network packet reception rates, and current pod counts. The system significantly outperforms traditional reactive HPA approaches by proactively scaling resources based on learned patterns and anomaly detection.


🔍 How It Works

The system uses a combination of three machine learning models:
- **Random Forest**: Provides robust prediction with low MSE (0.6333)
- **XGBoost**: Offers excellent generalization capabilities
- **Neural Network**: Captures complex non-linear relationships
- **Ensemble Approach**: Combines all models for optimal predictions

The autoscaler monitors key metrics including:
- CPU utilization
- Network packet reception rates
- Current pod counts
- Historical performance patterns

Based on these inputs, it predicts the optimal number of pods needed to handle the current and anticipated load.


## Dataset

This project uses the "Horizontal Scaling in Kubernetes Dataset Using Artificial Neural Networks for Load Forecasting" dataset (published July 23, 2024). The dataset is not included in this repository due to size constraints.

### Download Dataset
You can download the required dataset files from the following links:
- [TrainData.csv](https://prod-dcd-datasets-cache-zipfiles.s3.eu-west-1.amazonaws.com/ks9vbv5pb2-1.zip)
- [TestK8sData.csv](https://prod-dcd-datasets-cache-zipfiles.s3.eu-west-1.amazonaws.com/ks9vbv5pb2-1.zip)
- [TestJMeterData.csv](https://prod-dcd-datasets-cache-zipfiles.s3.eu-west-1.amazonaws.com/ks9vbv5pb2-1.zip)

After downloading, place these files in the `Dataset/` directory of this project.

### Dataset Description
The dataset provides comprehensive metrics collected from Kubernetes cluster experiments:
- **TrainData.csv**: Contains averaged metrics under various stress conditions
- **TestK8sData.csv**: Includes timestamped records of cluster performance
- **TestJMeterData.csv**: Provides detailed HTTP request logs with performance metrics

## ✨ Features

| Feature | Description |
|---------|-------------|
| **Ensemble Model Architecture** | Combines Random Forest, XGBoost, and Neural Network models |
| **Advanced Feature Engineering** | Creates polynomial features and interaction metrics |
| **Anomaly Detection** | Implements autoencoder-based detection of unusual system behavior |
| **Model Explainability** | Uses SHAP values to explain predictions and feature importance |
| **GPU Acceleration** | Utilizes TensorFlow and XGBoost GPU capabilities |
| **Mixed Precision Training** | Implements mixed precision for improved efficiency |

## 📊 Performance

Our ensemble model achieves exceptional performance metrics:

```
Random Forest: MSE: 0.6333, MAE: 0.3939, R²: 0.9992
XGBoost: MSE: 1.0622, MAE: 0.7256, R²: 0.9987
Neural Network: MSE: 1.7747, MAE: 1.0101, R²: 0.9978
Ensemble: MSE: 0.7254, MAE: 0.6131, R²: 0.9991
```

## Usage

### Predicting Stress Levels

```python
from k8s_autoscaler import predict_stress_level

# Predict stress level for given metrics
cpu_input = 75.0
packet_recv_input = 120.5
pods_number_input = 10

predicted_stress, status, confidence = predict_stress_level(
    models=[rf_model, xgb_model, nn_model],
    cpu=cpu_input,
    packet_recv=input_packet_recv,
    pods_number=pods_number_input,
    scaler=scaler
)

print(f"Predicted Stress Level: {predicted_stress:.2f}")
print(f"Status: {status}")
print(f"Confidence: {confidence:.2f}")
```

## Implementation Details

### Neural Network Architecture

```
Sequential([
    Dense(128, activation='relu', input_shape=(input_shape,)),
    BatchNormalization(),
    Dropout(0.3),
    Dense(64, activation='relu'),
    BatchNormalization(),
    Dropout(0.3),
    Dense(32, activation='relu'),
    BatchNormalization(),
    Dense(1)
])
```

### Anomaly Detection

The autoencoder architecture identifies anomalies in system behavior:
```
Encoder: Input(input_dim) → Dense(64) → Dense(32) → Dense(16)
Decoder: Dense(16) → Dense(32) → Dense(64) → Output(input_dim)
```

## Requirements

- Python
- TensorFlow
- scikit-learn
- XGBoost
- pandas, numpy, matplotlib, seaborn
- SHAP

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgments

- Dataset provided by Marcelo Fernandes through Mendeley Data
- Inspired by research in time-series forecasting for Kubernetes autoscaling
- Built upon the Kubernetes Horizontal Pod Autoscaler framework

Citations:
[1] https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/50481759/ac5ef4c7-e258-4bcf-830e-d3c88f45ecb5/paste.txt
[2] https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/50481759/a19e38fe-e609-4a69-98f0-f57fa2b246f7/paste-2.txt
[3] https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/50481759/b05dc624-9093-4887-85e7-a84d97fb45d5/paste-3.txt

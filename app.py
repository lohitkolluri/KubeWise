from flask import Flask, render_template, request
import joblib
import numpy as np
import pandas as pd
from sklearn.preprocessing import PolynomialFeatures, StandardScaler

# Load your best model from file
best_model = joblib.load("best_model.pkl")

# Re-create the polynomial transformer (as used during training)
poly = PolynomialFeatures(degree=2, include_bias=False)

# Load training data to re-fit the scaler (adjust the path as needed)
train_data = pd.read_csv("Dataset/TrainData.csv")
# Create base features from training data
X_train_raw = train_data[['CPU_Mean', 'PackRecv_Mean', 'PodsNumber_Mean']].copy()
X_train_raw['CPU_Pod_Ratio'] = X_train_raw['CPU_Mean'] / (X_train_raw['PodsNumber_Mean'] + 0.1)
X_train_raw['PackRecv_Pod_Ratio'] = X_train_raw['PackRecv_Mean'] / (X_train_raw['PodsNumber_Mean'] + 0.1)
X_train_raw['CPU_PackRecv_Product'] = X_train_raw['CPU_Mean'] * X_train_raw['PackRecv_Mean']

# Fit the polynomial transformer and scaler on training data
X_poly_train = poly.fit_transform(X_train_raw)
scaler = StandardScaler()
scaler.fit(X_poly_train)

def predict_stress(cpu, packet_recv, pods_number):
    # Create a DataFrame from user inputs
    input_data = pd.DataFrame({
        'CPU_Mean': [cpu],
        'PackRecv_Mean': [packet_recv],
        'PodsNumber_Mean': [pods_number]
    })
    # Create derived features
    input_data['CPU_Pod_Ratio'] = input_data['CPU_Mean'] / (input_data['PodsNumber_Mean'] + 0.1)
    input_data['PackRecv_Pod_Ratio'] = input_data['PackRecv_Mean'] / (input_data['PodsNumber_Mean'] + 0.1)
    input_data['CPU_PackRecv_Product'] = input_data['CPU_Mean'] * input_data['PackRecv_Mean']

    # Apply polynomial transformation and scaling
    input_poly = poly.transform(input_data)
    scaled_input = scaler.transform(input_poly)

    # Predict stress level using the loaded best model
    stress_level = best_model.predict(scaled_input)[0]

    # Determine qualitative status
    if stress_level < 20:
        status = "Normal operation"
    elif stress_level < 40:
        status = "Moderate stress"
    elif stress_level < 60:
        status = "High stress"
    else:
        status = "Critical stress"

    return stress_level, status

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/predict', methods=['POST'])
def predict():
    try:
        cpu = float(request.form['cpu'])
        packet_recv = float(request.form['packet_recv'])
        pods_number = float(request.form['pods_number'])
        stress_level, status = predict_stress(cpu, packet_recv, pods_number)
        result = {
            'stress_level': round(stress_level, 2),
            'status': status
        }
        return render_template('index.html', result=result)
    except Exception as e:
        return render_template('index.html', error=str(e))

if __name__ == '__main__':
    app.run(debug=True)

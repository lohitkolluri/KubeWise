# Kubernetes Prediction & Monitoring System

A full-stack application for monitoring Kubernetes clusters, predicting potential issues, and providing automated remediation actions.

## Project Structure

The project consists of two main components:

### Backend

- **Flask API**: Provides endpoints for metrics, cluster status, and remediation actions
- **Prometheus Integration**: Collects and processes metrics from Prometheus
- **Kubernetes Client**: Interacts with the Kubernetes API
- **Gemini Agent**: AI-powered analysis of cluster metrics (requires Google API key)

### Frontend

- **Next.js**: React-based frontend application
- **Shadcn/UI**: Modern UI components
- **Recharts**: Data visualization
- **Dashboard**: Overview of cluster health and metrics

## Setup & Installation

### Prerequisites

- Node.js (v18+)
- Python (v3.8+)
- Kubernetes cluster with Prometheus
- Google API Key (optional, for Gemini integration)

### Backend Setup

1. Navigate to the backend directory:
   ```bash
   cd backend
   ```

2. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Configure environment variables:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` to include your Prometheus URL and other settings.

5. Run the backend:
   ```bash
   export PROMETHEUS_URL=http://localhost:9090  # or your actual Prometheus URL
   python app.py
   ```

### Frontend Setup

1. Navigate to the frontend directory:
   ```bash
   cd frontend
   ```

2. Install dependencies:
   ```bash
   npm install
   ```

3. Run the frontend:
   ```bash
   npm run dev
   ```

## API Endpoints

- **GET /api/status**: Current cluster status
- **GET /api/metrics**: Detailed metrics
- **GET /api/agent/analyze**: AI-powered analysis and recommendations
- **GET /api/remediations**: History of remediation actions
- **POST /api/remediations**: Trigger a remediation action
- **POST /api/settings**: Update system settings

## Configuration

### Backend Configuration

Edit `.env` file in the backend directory to set:
- `PROMETHEUS_URL`: URL of your Prometheus server
- `GOOGLE_API_KEY`: For Gemini AI integration (optional)
- `KUBE_CONFIG_PATH`: Path to your Kubernetes config file

### Frontend Configuration

The frontend connects to the backend API which should be running on http://localhost:8000 by default.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

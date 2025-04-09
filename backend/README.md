# 🚀 Kubernetes Prediction Model

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100.0-green)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-available-blue)](https://www.docker.com/)

A real-time Kubernetes monitoring and prediction system that uses AI to analyze cluster metrics and predict potential issues before they occur.

## 🌟 Features

- **Real-time Monitoring**: Continuous monitoring of Kubernetes cluster metrics
- **AI-Powered Analysis**: Advanced analysis using Azure OpenAI for pattern detection
- **Predictive Alerts**: Early warning system for potential cluster issues
- **Email Notifications**: Configurable email alerts for critical issues
- **RESTful API**: Comprehensive API for monitoring and management
- **Supabase Integration**: Persistent storage and real-time updates
- **Docker Support**: Easy deployment with Docker containers

## 📋 Prerequisites

- Python 3.9+
- Kubernetes cluster access
- Azure OpenAI API key
- Supabase account
- SMTP server for email alerts

## 🛠️ Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/k8s-prediction-model.git
   cd k8s-prediction-model
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables**
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

4. **Run the application**
   ```bash
   uvicorn app.main:app --reload
   ```

## 🐳 Docker Deployment

```bash
# Build the image
docker build -t k8s-prediction-model .

# Run the container
docker run -d \
  --name k8s-prediction \
  -p 8000:8000 \
  -v ~/.kube:/root/.kube \
  --env-file .env \
  k8s-prediction-model
```

## ⚙️ Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `KUBECONFIG_PATH` | Path to kubeconfig file | `/root/.kube/config` |
| `MONITOR_INTERVAL_SECONDS` | Monitoring interval | `60` |
| `MONITORED_NAMESPACES` | Namespaces to monitor | `*` |
| `SUPABASE_URL` | Supabase project URL | - |
| `SUPABASE_KEY` | Supabase API key | - |
| `REDIS_URL` | Redis connection URL | - |
| `AI_PROVIDER` | AI service provider | `azure` |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI API key | - |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI endpoint | - |
| `SMTP_HOST` | SMTP server host | - |
| `SMTP_PORT` | SMTP server port | `587` |

## 📚 API Documentation

Once the application is running, visit:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### Key Endpoints

- `GET /api/v1/metrics` - Get current cluster metrics
- `GET /api/v1/issues` - List detected issues
- `GET /api/v1/predictions` - Get AI predictions
- `POST /api/v1/analyze` - Trigger manual analysis

## 🔍 Monitoring

The system monitors various Kubernetes metrics:
- Pod resource usage
- Node health
- Deployment status
- Service availability
- Network metrics

## 🤖 AI Analysis

The AI analyzer:
- Processes cluster metrics
- Identifies patterns and anomalies
- Generates predictions
- Provides remediation suggestions

## 📧 Alerting

Configure email alerts in `.env`:
```env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=your-email@example.com
SMTP_PASSWORD=your-password
ALERT_SENDER_EMAIL=alerts@example.com
ALERT_RECIPIENT_EMAIL=admin@example.com
```

## 🛡️ Security

- API key authentication
- TLS encryption for SMTP
- Secure storage of credentials
- Role-based access control

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- [FastAPI](https://fastapi.tiangolo.com/)
- [Kubernetes Python Client](https://github.com/kubernetes-client/python)
- [Azure OpenAI](https://azure.microsoft.com/en-us/products/ai-services/openai-service)
- [Supabase](https://supabase.com/)

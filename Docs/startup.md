## KubeWise FastAPI Startup

- Custom ASCII banner rendered using `rich`
- Dynamic spinner-based loading simulation for:
  - Server Core
  - Connecting to Kubernetes cluster
  - Fetching cluster nodes (and listing them)
  - Prometheus Scraper
  - Anomaly Detection Model
  - Remediation Agents
- Color-coded logs for better developer experience
- Smooth startup visual using `rich.progress`
- Graceful handling if Kubernetes connection fails during startup

#### Install Requirements
```
pip install fastapi uvicorn rich kubernetes
```

#### Run Server
```
uvicorn kubewise.api.server:create_app --reload --factory

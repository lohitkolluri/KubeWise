# KubeWise Helm Chart

This Helm chart deploys the KubeWise application - an AI-Powered Kubernetes Anomaly Detection and Autonomous Remediation System - on a Kubernetes cluster.

## Prerequisites

- Kubernetes 1.16+
- Helm 3.0+

## Installing the Chart

To install the chart with the release name `my-kubewise`:

```bash
# Clone the repository
git clone https://github.com/lohitkolluri/KubeWise.git
cd KubeWise

# Install the chart
helm install my-kubewise ./helm/kubewise --set mongodb.uri=<your-mongodb-uri>
```

## Uninstalling the Chart

To uninstall/delete the `my-kubewise` deployment:

```bash
helm uninstall my-kubewise
```

## Configuration

The following table lists the configurable parameters of the KubeWise chart and their default values.

| Parameter                | Description                                     | Default                |
|--------------------------|-------------------------------------------------|------------------------|
| `replicaCount`           | Number of replicas for the deployment           | `1`                    |
| `image.repository`       | Image repository                                | `kubewise`             |
| `image.tag`              | Image tag                                       | `latest`               |
| `image.pullPolicy`       | Image pull policy                               | `IfNotPresent`         |
| `service.type`           | Kubernetes Service type                         | `ClusterIP`            |
| `service.port`           | Service port                                    | `80`                   |
| `service.targetPort`     | Container port                                  | `8000`                 |
| `mongodb.uri`            | MongoDB connection URI (required)               | `""`                   |
| `resources`              | CPU/Memory resource requests/limits             | See `values.yaml`      |
| `nodeSelector`           | Node labels for pod assignment                  | `{}`                   |
| `tolerations`            | Tolerations for pod assignment                  | `[]`                   |
| `affinity`               | Affinity for pod assignment                     | `{}`                   |

### Note
The application will automatically retrieve other environment variables (like database name, API keys, log level, etc.) from the MongoDB database once connected.

### Example values.yaml

You can create a custom `values.yaml` file to override the default values:

```yaml
replicaCount: 2

image:
  repository: myregistry/kubewise
  tag: v1.0.0
  pullPolicy: Always

service:
  type: LoadBalancer
  port: 80

mongodb:
  uri: mongodb+srv://username:password@cluster.mongodb.net

resources:
  limits:
    cpu: 2000m
    memory: 2Gi
  requests:
    cpu: 1000m
    memory: 1Gi
```

Then install the chart with:

```bash
helm install my-kubewise ./helm/kubewise -f my-values.yaml
```

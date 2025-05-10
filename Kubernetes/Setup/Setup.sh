#!/bin/bash
set -e

# 1. Create monitoring namespace (if not exists)
kubectl apply -f 00-namespace.yaml

# 2. Clean up conflicting ClusterRoles/ClusterRoleBindings from previous installs
for cr in $(kubectl get clusterrole | grep kube-prometheus-stack | awk '{print $1}'); do
  kubectl delete clusterrole $cr || true
done
for crb in $(kubectl get clusterrolebinding | grep kube-prometheus-stack | awk '{print $1}'); do
  kubectl delete clusterrolebinding $crb || true
done

# 3. Install kube-prometheus-stack via Helm
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts || true
helm repo update
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace

# 4. Wait for Prometheus Operator and core pods to be ready
kubectl rollout status deployment/kube-prometheus-stack-operator -n monitoring
kubectl rollout status deployment/kube-prometheus-stack-grafana -n monitoring

# Wait for Prometheus StatefulSet to be created
PROM_STS="prometheus-kube-prometheus-stack-prometheus"
for i in {1..30}; do
  if kubectl get statefulset/$PROM_STS -n monitoring &>/dev/null; then
    echo "Found StatefulSet $PROM_STS."
    break
  fi
  echo "Waiting for StatefulSet $PROM_STS to be created... ($i/30)"
  sleep 10
done
if ! kubectl get statefulset/$PROM_STS -n monitoring &>/dev/null; then
  echo "Error: StatefulSet $PROM_STS was not created after waiting. Exiting."
  exit 1
fi
kubectl rollout status statefulset/$PROM_STS -n monitoring

kubectl rollout status statefulset/alertmanager-kube-prometheus-stack-alertmanager -n monitoring

# 5. Apply cAdvisor, custom exporters, and CoreDNS metrics
kubectl apply -f 08-cadvisor.yaml
kubectl apply -f 13-custom-exporter.yaml
kubectl apply -f 09-coredns-metrics.yaml

echo "Monitoring stack setup complete!"
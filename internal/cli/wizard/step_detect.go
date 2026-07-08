package wizard

import (
	"context"
	"os/exec"
	"strings"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/clientcmd"
)

// ClusterInfo holds detected cluster metadata.
type ClusterInfo struct {
	Context             string
	Type                string // kind, eks, aks, gke, bare-metal
	ServerVersion       string
	HelmInstalled       bool
	PrometheusService   string
	PrometheusNamespace string
	DetectedAt          time.Time
}

// detectCluster performs auto-detection of the Kubernetes environment.
// All errors are non-fatal — best-effort detection with graceful fallbacks.
func detectCluster(ctx context.Context) (ClusterInfo, error) {
	info := ClusterInfo{DetectedAt: time.Now()}

	// 1. Check kubeconfig context.
	loadingRules := clientcmd.NewDefaultClientConfigLoadingRules()
	configOverrides := &clientcmd.ConfigOverrides{}
	kubeConfig := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(loadingRules, configOverrides)
	raw, err := kubeConfig.RawConfig()
	if err == nil {
		info.Context = raw.CurrentContext
	}

	// 2. Connect to cluster and detect type.
	restCfg, err := kubeConfig.ClientConfig()
	if err != nil {
		// kubeconfig not available — return what we have.
		return info, nil
	}

	cs, err := kubernetes.NewForConfig(restCfg)
	if err != nil {
		return info, nil
	}

	// 3. Server version.
	sv, err := cs.Discovery().ServerVersion()
	if err == nil {
		info.ServerVersion = sv.GitVersion
	}

	// 4. Detect cluster type from node labels.
	detectCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	nodes, err := cs.CoreV1().Nodes().List(detectCtx, metav1.ListOptions{Limit: 1})
	if err == nil {
		for _, n := range nodes.Items {
			switch {
			case n.Labels["eks.amazonaws.com/compute-type"] != "":
				info.Type = "eks"
			case n.Labels["cloud.google.com/gke-nodepool"] != "":
				info.Type = "gke"
			case n.Labels["kubernetes.azure.com/cluster"] != "":
				info.Type = "aks"
			case n.Labels["kind.x-k8s.io/cluster-name"] != "":
				info.Type = "kind"
			case n.Labels["minikube.k8s.io/name"] != "":
				info.Type = "minikube"
			default:
				// Check kubeconfig server for managed-k8s indicators.
				if strings.Contains(info.Context, "eks") || strings.Contains(info.Context, "eks") {
					info.Type = "eks"
				} else if strings.Contains(info.Context, "gke") {
					info.Type = "gke"
				} else if strings.Contains(info.Context, "aks") {
					info.Type = "aks"
				}
			}
		}
	}
	if info.Type == "" {
		info.Type = "bare-metal"
	}

	// 5. Check helm availability.
	info.HelmInstalled = exec.Command("helm", "version", "--short").Run() == nil

	// 6. Detect Prometheus in common namespaces.
	promNamespaces := []string{"monitoring", "kube-system", "default", "prometheus"}
	promCtx, cancelProm := context.WithTimeout(ctx, 10*time.Second)
	defer cancelProm()
	for _, ns := range promNamespaces {
		svcs, err := cs.CoreV1().Services(ns).List(promCtx, metav1.ListOptions{
			LabelSelector: "app.kubernetes.io/name=prometheus",
		})
		if err == nil && len(svcs.Items) > 0 {
			info.PrometheusService = svcs.Items[0].Name
			info.PrometheusNamespace = ns
			break
		}
		// Fallback: search by name.
		if info.PrometheusService == "" {
			svcs, err := cs.CoreV1().Services(ns).List(promCtx, metav1.ListOptions{})
			if err == nil {
				for _, svc := range svcs.Items {
					if strings.Contains(svc.Name, "prometheus") {
						info.PrometheusService = svc.Name
						info.PrometheusNamespace = ns
						break
					}
				}
			}
		}
		if info.PrometheusService != "" {
			break
		}
	}

	return info, nil
}

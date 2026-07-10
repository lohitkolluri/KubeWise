package wizard

import (
	"time"
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



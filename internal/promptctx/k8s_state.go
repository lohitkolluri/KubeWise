package promptctx

import (
	"crypto/sha256"
	"fmt"
)

// HashK8sState computes a content hash of a K8sState for change detection.
// The Build() method stores this hash alongside the context; the caller
// compares it against the previous hash to skip expensive re-population.
func HashK8sState(s *K8sState) string {
	if s == nil {
		return ""
	}
	h := sha256.New()
	if s.Deployment != nil {
		h.Write([]byte(s.Deployment.Name))
		h.Write([]byte(fmt.Sprintf("%d", s.Deployment.Replicas)))
		h.Write([]byte(fmt.Sprintf("%d", s.Deployment.Available)))
		h.Write([]byte(s.Deployment.Strategy))
		h.Write([]byte(s.Deployment.Image))
	}
	if s.Node != nil {
		h.Write([]byte(s.Node.Name))
		if s.Node.Ready {
			h.Write([]byte("ready"))
		} else {
			h.Write([]byte("not_ready"))
		}
		for _, c := range s.Node.Conditions {
			h.Write([]byte(c.Type))
			h.Write([]byte(c.Status))
		}
	}
	if s.Pod != nil {
		h.Write([]byte(s.Pod.Name))
		h.Write([]byte(s.Pod.Phase))
		h.Write([]byte(fmt.Sprintf("%d", s.Pod.Restarts)))
		for _, c := range s.Pod.Container {
			h.Write([]byte(c.Name))
			h.Write([]byte(c.State))
		}
	}
	return fmt.Sprintf("%x", h.Sum(nil))
}

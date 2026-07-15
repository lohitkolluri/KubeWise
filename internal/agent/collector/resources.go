package collector

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/apimachinery/pkg/util/wait"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/cache"

	nsutil "github.com/lohitkolluri/KubeWise/pkg/namespace"
)

// PodState represents a snapshot of a pod's current state.
type PodState struct {
	Name          string   `json:"name"`
	Namespace     string   `json:"namespace"`
	Phase         string   `json:"phase"`
	Ready         bool     `json:"ready"`
	RestartCount  int32    `json:"restart_count"`
	MemLimitBytes float64  `json:"mem_limit_bytes"`
	Conditions    []string `json:"conditions,omitempty"`
	OwnerKind     string   `json:"owner_kind,omitempty"`
	OwnerName     string   `json:"owner_name,omitempty"`
}

// NodeState represents a snapshot of a node's current state.
type NodeState struct {
	Name       string   `json:"name"`
	Ready      bool     `json:"ready"`
	Conditions []string `json:"conditions,omitempty"`
}

// DeploymentState represents a snapshot of a deployment's current state.
type DeploymentState struct {
	Name              string `json:"name"`
	Namespace         string `json:"namespace"`
	AvailableReplicas int32  `json:"available_replicas"`
	Replicas          int32  `json:"replicas"`
}

// ResourcesCollector uses K8s informers to track live resource state.
type ResourcesCollector struct {
	clientset       kubernetes.Interface
	watchNamespaces []string

	mu    sync.RWMutex
	pods  map[string]PodState
	nodes map[string]NodeState
	deps  map[string]DeploymentState

	podInformer  cache.Controller
	nodeInformer cache.Controller
	depInformer  cache.Controller

	synced      bool
	syncTimeout time.Duration
}

// NewResourcesCollector creates and starts resource informers.
func NewResourcesCollector(clientset kubernetes.Interface, watchNamespaces []string) *ResourcesCollector {
	rc := &ResourcesCollector{
		clientset:       clientset,
		watchNamespaces: watchNamespaces,
		pods:            make(map[string]PodState),
		nodes:           make(map[string]NodeState),
		deps:            make(map[string]DeploymentState),
		syncTimeout:     30 * time.Second,
	}
	rc.startInformers()
	return rc
}

func (rc *ResourcesCollector) startInformers() {
	// Pod watcher
	podListWatch := cache.NewListWatchFromClient(
		rc.clientset.CoreV1().RESTClient(),
		"pods",
		"",
		fields.Everything(),
	)
	_, rc.podInformer = cache.NewInformer(
		podListWatch,
		&corev1.Pod{},
		time.Minute,
		cache.ResourceEventHandlerFuncs{
			AddFunc:    rc.handlePodAdd,
			UpdateFunc: rc.handlePodUpdate,
			DeleteFunc: rc.handlePodDelete,
		},
	)

	// Node watcher
	nodeListWatch := cache.NewListWatchFromClient(
		rc.clientset.CoreV1().RESTClient(),
		"nodes",
		"",
		fields.Everything(),
	)
	_, rc.nodeInformer = cache.NewInformer(
		nodeListWatch,
		&corev1.Node{},
		time.Minute,
		cache.ResourceEventHandlerFuncs{
			AddFunc:    rc.handleNodeAdd,
			UpdateFunc: rc.handleNodeUpdate,
			DeleteFunc: rc.handleNodeDelete,
		},
	)

	// Deployment watcher (via apps/v1 API)
	depListWatch := cache.NewListWatchFromClient(
		rc.clientset.AppsV1().RESTClient(),
		"deployments",
		"",
		fields.Everything(),
	)
	_, rc.depInformer = cache.NewInformer(
		depListWatch,
		&appsv1.Deployment{},
		time.Minute,
		cache.ResourceEventHandlerFuncs{
			AddFunc:    rc.handleDepAdd,
			UpdateFunc: rc.handleDepUpdate,
			DeleteFunc: rc.handleDepDelete,
		},
	)
}

// Run starts all informers and blocks until ctx is cancelled.
func (rc *ResourcesCollector) Run(ctx context.Context) {
	go rc.podInformer.Run(ctx.Done())
	go rc.nodeInformer.Run(ctx.Done())
	go rc.depInformer.Run(ctx.Done())

	if !cache.WaitForCacheSync(ctx.Done(), rc.podInformer.HasSynced, rc.nodeInformer.HasSynced, rc.depInformer.HasSynced) {
		slog.Warn("resources: informer cache sync timed out, retrying")
		if err := wait.PollUntilContextTimeout(ctx, 2*time.Second, rc.syncTimeout, true, func(ctx context.Context) (bool, error) {
			return cache.WaitForCacheSync(ctx.Done(), rc.podInformer.HasSynced, rc.nodeInformer.HasSynced, rc.depInformer.HasSynced), nil
		}); err != nil && !errors.Is(err, context.Canceled) {
			slog.Error("resources: informer cache sync failed after retry")
			return
		}
	}
	rc.mu.Lock()
	rc.synced = true
	rc.mu.Unlock()
}

// HasSynced returns true once all informers have synced.
func (rc *ResourcesCollector) HasSynced() bool {
	rc.mu.RLock()
	defer rc.mu.RUnlock()
	return rc.synced
}

// WaitForSync blocks up to the timeout duration until all informers have synced.
func (rc *ResourcesCollector) WaitForSync() bool {
	err := wait.PollUntilContextTimeout(context.Background(), 100*time.Millisecond, rc.syncTimeout, true, func(_ context.Context) (bool, error) {
		return rc.HasSynced(), nil
	})
	return err == nil
}

// Snapshot returns a consistent point-in-time view of resource state.
func (rc *ResourcesCollector) Snapshot() (failing []PodState, unhealthy []string, pods []PodState) {
	rc.mu.RLock()
	defer rc.mu.RUnlock()
	for _, p := range rc.pods {
		if !nsutil.InScope(p.Namespace, rc.watchNamespaces) {
			continue
		}
		if p.Phase == string(corev1.PodSucceeded) {
			pods = append(pods, p)
			continue
		}
		if p.Phase != string(corev1.PodRunning) || !p.Ready {
			failing = append(failing, p)
		}
		pods = append(pods, p)
	}
	for _, n := range rc.nodes {
		if !n.Ready {
			unhealthy = append(unhealthy, n.Name)
		}
	}
	return failing, unhealthy, pods
}

// GetPodOwner returns the controlling owner kind and name for a pod, or ("", "") if not found.
func (rc *ResourcesCollector) GetPodOwner(namespace, name string) (kind, ownerName string) {
	rc.mu.RLock()
	defer rc.mu.RUnlock()
	for _, p := range rc.pods {
		if p.Namespace == namespace && p.Name == name {
			return p.OwnerKind, p.OwnerName
		}
	}
	return "", ""
}

// GetFailingPods returns pods that are not Running or are Running but not Ready.
func (rc *ResourcesCollector) GetFailingPods() []PodState {
	failing, _, _ := rc.Snapshot()
	return failing
}

// GetUnhealthyNodes returns nodes that are not Ready.
func (rc *ResourcesCollector) GetUnhealthyNodes() []NodeState {
	rc.mu.RLock()
	defer rc.mu.RUnlock()
	var unhealthy []NodeState
	for _, n := range rc.nodes {
		if !n.Ready {
			unhealthy = append(unhealthy, n)
		}
	}
	return unhealthy
}

// --- Pod handlers ---

func podKey(p *corev1.Pod) string {
	return fmt.Sprintf("%s/%s", p.Namespace, p.Name)
}

func (rc *ResourcesCollector) handlePodAdd(obj interface{}) {
	pod, ok := obj.(*corev1.Pod)
	if !ok {
		return
	}
	rc.mu.Lock()
	defer rc.mu.Unlock()
	rc.pods[podKey(pod)] = podToState(pod)
}

func (rc *ResourcesCollector) handlePodUpdate(_, newObj interface{}) {
	pod, ok := newObj.(*corev1.Pod)
	if !ok {
		return
	}
	rc.mu.Lock()
	defer rc.mu.Unlock()
	rc.pods[podKey(pod)] = podToState(pod)
}

func (rc *ResourcesCollector) handlePodDelete(obj interface{}) {
	pod, ok := obj.(*corev1.Pod)
	if !ok {
		if tombstone, tombOK := obj.(cache.DeletedFinalStateUnknown); tombOK {
			pod, ok = tombstone.Obj.(*corev1.Pod)
		}
	}
	if !ok || pod == nil {
		return
	}
	rc.mu.Lock()
	defer rc.mu.Unlock()
	delete(rc.pods, podKey(pod))
}

func podToState(pod *corev1.Pod) PodState {
	s := PodState{
		Name:      pod.Name,
		Namespace: pod.Namespace,
		Phase:     string(pod.Status.Phase),
	}
	// Extract controlling owner reference for workload correlation.
	for _, ref := range pod.OwnerReferences {
		if ref.Controller != nil && *ref.Controller {
			s.OwnerKind = ref.Kind
			s.OwnerName = ref.Name
			break
		}
	}
	if s.OwnerKind == "" && len(pod.OwnerReferences) > 0 {
		ref := pod.OwnerReferences[0]
		s.OwnerKind = ref.Kind
		s.OwnerName = ref.Name
	}
	for _, c := range pod.Status.Conditions {
		if c.Type == corev1.PodReady {
			s.Ready = c.Status == corev1.ConditionTrue
		}
		s.Conditions = append(s.Conditions, fmt.Sprintf("%s=%s", c.Type, c.Status))
	}
	for _, cs := range pod.Status.ContainerStatuses {
		s.RestartCount += cs.RestartCount
	}
	for _, c := range pod.Spec.Containers {
		if c.Resources.Limits != nil {
			if mem, ok := c.Resources.Limits[corev1.ResourceMemory]; ok {
				s.MemLimitBytes += float64(mem.Value())
			}
		}
	}
	for _, ref := range pod.OwnerReferences {
		if ref.Controller != nil && *ref.Controller {
			s.OwnerKind = ref.Kind
			s.OwnerName = ref.Name
			break
		}
	}
	return s
}

// --- Node handlers ---

func (rc *ResourcesCollector) handleNodeAdd(obj interface{}) {
	node, ok := obj.(*corev1.Node)
	if !ok {
		return
	}
	rc.mu.Lock()
	defer rc.mu.Unlock()
	rc.nodes[node.Name] = nodeToState(node)
}

func (rc *ResourcesCollector) handleNodeUpdate(_, newObj interface{}) {
	node, ok := newObj.(*corev1.Node)
	if !ok {
		return
	}
	rc.mu.Lock()
	defer rc.mu.Unlock()
	rc.nodes[node.Name] = nodeToState(node)
}

func (rc *ResourcesCollector) handleNodeDelete(obj interface{}) {
	node, ok := obj.(*corev1.Node)
	if !ok {
		if tombstone, tombOK := obj.(cache.DeletedFinalStateUnknown); tombOK {
			node, ok = tombstone.Obj.(*corev1.Node)
		}
	}
	if !ok || node == nil {
		return
	}
	rc.mu.Lock()
	defer rc.mu.Unlock()
	delete(rc.nodes, node.Name)
}

func nodeToState(node *corev1.Node) NodeState {
	s := NodeState{Name: node.Name}
	for _, c := range node.Status.Conditions {
		if c.Type == corev1.NodeReady {
			s.Ready = c.Status == corev1.ConditionTrue
		}
		s.Conditions = append(s.Conditions, fmt.Sprintf("%s=%s", c.Type, c.Status))
	}
	return s
}

// --- Deployment handlers ---

func depKey(d *appsv1.Deployment) string {
	return fmt.Sprintf("%s/%s", d.Namespace, d.Name)
}

func (rc *ResourcesCollector) handleDepAdd(obj interface{}) {
	dep, ok := obj.(*appsv1.Deployment)
	if !ok {
		return
	}
	rc.mu.Lock()
	defer rc.mu.Unlock()
	rc.deps[depKey(dep)] = deploymentToState(dep)
}

func (rc *ResourcesCollector) handleDepUpdate(_, newObj interface{}) {
	dep, ok := newObj.(*appsv1.Deployment)
	if !ok {
		return
	}
	rc.mu.Lock()
	defer rc.mu.Unlock()
	rc.deps[depKey(dep)] = deploymentToState(dep)
}

func (rc *ResourcesCollector) handleDepDelete(obj interface{}) {
	dep, ok := obj.(*appsv1.Deployment)
	if !ok {
		if tombstone, tombOK := obj.(cache.DeletedFinalStateUnknown); tombOK {
			dep, ok = tombstone.Obj.(*appsv1.Deployment)
		}
	}
	if !ok || dep == nil {
		return
	}
	rc.mu.Lock()
	defer rc.mu.Unlock()
	delete(rc.deps, depKey(dep))
}

func deploymentToState(dep *appsv1.Deployment) DeploymentState {
	return DeploymentState{
		Name:              dep.Name,
		Namespace:         dep.Namespace,
		AvailableReplicas: dep.Status.AvailableReplicas,
		Replicas:          dep.Status.Replicas,
	}
}

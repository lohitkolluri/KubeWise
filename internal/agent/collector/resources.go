package collector

import (
	"context"
	"fmt"
	"log"
	"sync"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/apimachinery/pkg/util/wait"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/cache"
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
	clientset kubernetes.Interface

	mu    sync.RWMutex
	pods  map[string]PodState
	nodes map[string]NodeState
	deps  map[string]DeploymentState

	podInformer  cache.Controller
	nodeInformer cache.Controller
	depInformer  cache.Controller

	synced        bool
	syncTimeout   time.Duration
}

// NewResourcesCollector creates and starts resource informers.
func NewResourcesCollector(clientset kubernetes.Interface) *ResourcesCollector {
	rc := &ResourcesCollector{
		clientset:   clientset,
		pods:        make(map[string]PodState),
		nodes:       make(map[string]NodeState),
		deps:        make(map[string]DeploymentState),
		syncTimeout: 30 * time.Second,
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
		log.Printf("resources: informer cache sync timed out")
		return
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
	err := wait.PollImmediate(100*time.Millisecond, rc.syncTimeout, func() (bool, error) {
		return rc.HasSynced(), nil
	})
	return err == nil
}

// GetFailingPods returns pods that are not Running/Ready or are in a failed phase.
func (rc *ResourcesCollector) GetFailingPods() []PodState {
	rc.mu.RLock()
	defer rc.mu.RUnlock()
	var failing []PodState
	for _, p := range rc.pods {
		if p.Phase != string(corev1.PodRunning) {
			failing = append(failing, p)
			continue
		}
		if !p.Ready && p.RestartCount > 0 {
			failing = append(failing, p)
		}
	}
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

// GetAllPods returns all tracked pods.
func (rc *ResourcesCollector) GetAllPods() []PodState {
	rc.mu.RLock()
	defer rc.mu.RUnlock()
	pods := make([]PodState, 0, len(rc.pods))
	for _, p := range rc.pods {
		pods = append(pods, p)
	}
	return pods
}

// GetAllNodes returns all tracked nodes.
func (rc *ResourcesCollector) GetAllNodes() []NodeState {
	rc.mu.RLock()
	defer rc.mu.RUnlock()
	nodes := make([]NodeState, 0, len(rc.nodes))
	for _, n := range rc.nodes {
		nodes = append(nodes, n)
	}
	return nodes
}

// GetAllDeployments returns all tracked deployments.
func (rc *ResourcesCollector) GetAllDeployments() []DeploymentState {
	rc.mu.RLock()
	defer rc.mu.RUnlock()
	deps := make([]DeploymentState, 0, len(rc.deps))
	for _, d := range rc.deps {
		deps = append(deps, d)
	}
	return deps
}

// GetPodResources returns memory limits for all tracked pods.
func (rc *ResourcesCollector) GetPodResources() []PodState {
	rc.mu.RLock()
	defer rc.mu.RUnlock()
	pods := make([]PodState, 0, len(rc.pods))
	for _, p := range rc.pods {
		pods = append(pods, p)
	}
	return pods
}

// --- Pod handlers ---

func podKey(p *corev1.Pod) string {
	return fmt.Sprintf("%s/%s", p.Namespace, p.Name)
}

func (rc *ResourcesCollector) handlePodAdd(obj interface{}) {
	pod := obj.(*corev1.Pod)
	rc.mu.Lock()
	defer rc.mu.Unlock()
	rc.pods[podKey(pod)] = podToState(pod)
}

func (rc *ResourcesCollector) handlePodUpdate(oldObj, newObj interface{}) {
	pod := newObj.(*corev1.Pod)
	rc.mu.Lock()
	defer rc.mu.Unlock()
	rc.pods[podKey(pod)] = podToState(pod)
}

func (rc *ResourcesCollector) handlePodDelete(obj interface{}) {
	pod := obj.(*corev1.Pod)
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
	return s
}

// --- Node handlers ---

func (rc *ResourcesCollector) handleNodeAdd(obj interface{}) {
	node := obj.(*corev1.Node)
	rc.mu.Lock()
	defer rc.mu.Unlock()
	rc.nodes[node.Name] = nodeToState(node)
}

func (rc *ResourcesCollector) handleNodeUpdate(oldObj, newObj interface{}) {
	node := newObj.(*corev1.Node)
	rc.mu.Lock()
	defer rc.mu.Unlock()
	rc.nodes[node.Name] = nodeToState(node)
}

func (rc *ResourcesCollector) handleNodeDelete(obj interface{}) {
	node := obj.(*corev1.Node)
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
	dep := obj.(*appsv1.Deployment)
	rc.mu.Lock()
	defer rc.mu.Unlock()
	rc.deps[depKey(dep)] = deploymentToState(dep)
}

func (rc *ResourcesCollector) handleDepUpdate(oldObj, newObj interface{}) {
	dep := newObj.(*appsv1.Deployment)
	rc.mu.Lock()
	defer rc.mu.Unlock()
	rc.deps[depKey(dep)] = deploymentToState(dep)
}

func (rc *ResourcesCollector) handleDepDelete(obj interface{}) {
	dep := obj.(*appsv1.Deployment)
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

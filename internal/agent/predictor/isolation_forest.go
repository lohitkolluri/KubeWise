package predictor

import (
	"math"
	"math/rand"
	"sort"
)

// IFConfig configures the Isolation Forest detector.
type IFConfig struct {
	NTrees       int     // number of trees (default 100)
	SampleSize   int     // points sampled per tree (default 256)
	Threshold    float64 // anomaly threshold [0,1] (default 0.6)
}

// isolationForest is a lightweight online anomaly detector.
// Builds multiple random trees; anomalies have shorter average path lengths.
type isolationForest struct {
	trees     []ifTree
	cfg       IFConfig
	rng       *rand.Rand
	buffer    []float64 // rolling feature buffer (single feature for simplicity)
	bufIdx    int
	bufFull  bool
	featureFn func(float64) []float64
}

type ifTree struct {
	root     *ifNode
	size     int
	heightLimit int
}

type ifNode struct {
	left     *ifNode
	right    *ifNode
	splitIdx int
	splitVal float64
	size     int
	isLeaf   bool
}

func newIsolationForest(cfg IFConfig) *isolationForest {
	if cfg.NTrees <= 0 {
		cfg.NTrees = 100
	}
	if cfg.SampleSize <= 0 {
		cfg.SampleSize = 256
	}
	if cfg.Threshold <= 0 {
		cfg.Threshold = 0.6
	}
	return &isolationForest{
		cfg:    cfg,
		rng:    rand.New(rand.NewSource(int64(rand.Int()))),
		buffer: make([]float64, cfg.SampleSize),
	}
}

// Add feeds a new value to the rolling buffer and returns an anomaly score [0,1].
// The forest is rebuilt every SampleSize points.
func (f *isolationForest) Add(value float64) float64 {
	f.buffer[f.bufIdx] = value
	f.bufIdx++
	if f.bufIdx >= f.cfg.SampleSize {
		f.bufIdx = 0
		f.bufFull = true
	}
	if !f.bufFull {
		return 0 // not enough data yet
	}

	// Rebuild trees periodically (every SampleSize points, rebuild 1 tree)
	if len(f.trees) < f.cfg.NTrees {
		t := buildTree(f.buffer[:], 0, f.heightLimit())
		f.trees = append(f.trees, t)
	} else {
		// Replace a random tree with a fresh one (rolling update)
		idx := f.rng.Intn(len(f.trees))
		f.trees[idx] = buildTree(f.buffer[:], 0, f.heightLimit())
	}

	return f.score(value)
}

func (f *isolationForest) heightLimit() int {
	return int(math.Ceil(math.Log2(float64(f.cfg.SampleSize))))
}

func buildTree(samples []float64, depth, limit int) ifTree {
	if depth >= limit || len(samples) <= 1 {
		return ifTree{
			root: &ifNode{isLeaf: true, size: len(samples)},
			size: len(samples),
			heightLimit: limit,
		}
	}

	minVal, maxVal := samples[0], samples[0]
	for _, v := range samples {
		if v < minVal {
			minVal = v
		}
		if v > maxVal {
			maxVal = v
		}
	}

	if maxVal-minVal < 1e-10 {
		return ifTree{
			root: &ifNode{isLeaf: true, size: len(samples)},
			size: len(samples),
			heightLimit: limit,
		}
	}

	splitVal := minVal + rand.Float64()*(maxVal-minVal)
	var left, right []float64
	for _, v := range samples {
		if v < splitVal {
			left = append(left, v)
		} else {
			right = append(right, v)
		}
	}

	if len(left) == 0 || len(right) == 0 {
		return ifTree{
			root: &ifNode{isLeaf: true, size: len(samples)},
			size: len(samples),
			heightLimit: limit,
		}
	}

	lt := buildTree(left, depth+1, limit)
	rt := buildTree(right, depth+1, limit)

	return ifTree{
		root: &ifNode{
			left:     lt.root,
			right:    rt.root,
			splitVal: splitVal,
			isLeaf:   false,
			size:     len(samples),
		},
		size: len(samples),
		heightLimit: limit,
	}
}

func (f *isolationForest) score(value float64) float64 {
	if len(f.trees) == 0 {
		return 0
	}
	totalPath := 0.0
	for _, t := range f.trees {
		totalPath += float64(pathLength(t.root, value, 0))
	}
	avgPath := totalPath / float64(len(f.trees))
	// Normalize: score = 2^(-avgPath / c(n))
	c := cFactor(float64(f.cfg.SampleSize))
	normalized := math.Pow(2, -avgPath/c)
	if normalized > 1 {
		return 1
	}
	if normalized < 0 {
		return 0
	}
	return normalized
}

func pathLength(n *ifNode, value float64, depth int) int {
	if n == nil || n.isLeaf {
		return depth + int(cFactor(float64(n.size)))
	}
	if value < n.splitVal {
		return pathLength(n.left, value, depth+1)
	}
	return pathLength(n.right, value, depth+1)
}

func cFactor(n float64) float64 {
	if n <= 1 {
		return 0
	}
	if n <= 2 {
		return 1
	}
	h := math.Log(n-1) + 0.5772156649 // Euler-Mascheroni constant
	return 2*h - (2*(n-1))/n
}

// IFAnomalyScore computes the Isolation Forest score for a value
// given its recent history. Returns 0 if insufficient data.
func IFAnomalyScore(value float64, history []float64, nTrees, sampleSize int) float64 {
	if len(history) < sampleSize {
		return 0
	}
	if nTrees <= 0 {
		nTrees = 100
	}
	if sampleSize <= 0 {
		sampleSize = 256
	}
	// Use last sampleSize points from history
	samples := history
	if len(samples) > sampleSize {
		samples = samples[len(samples)-sampleSize:]
	}

	// Sort samples to make tree building deterministic-ish
	sorted := make([]float64, len(samples))
	copy(sorted, samples)
	sort.Float64s(sorted)

	// Build ensemble
	limit := int(math.Ceil(math.Log2(float64(sampleSize))))
	totalPath := 0.0
	for i := 0; i < nTrees; i++ {
		// Use different random seeds per tree for diversity
		rng := rand.New(rand.NewSource(int64(i * 9999)))
		t := buildTreeWithRng(sorted, 0, limit, rng)
		totalPath += float64(pathLength(t.root, value, 0))
	}
	avgPath := totalPath / float64(nTrees)
	c := cFactor(float64(sampleSize))
	score := math.Pow(2, -avgPath/c)
	if score > 1 {
		return 1
	}
	if score < 0 {
		return 0
	}
	return score
}

// buildTreeWithRng is like buildTree but uses a provided RNG for reproducibility.
func buildTreeWithRng(samples []float64, depth, limit int, rng *rand.Rand) ifTree {
	if depth >= limit || len(samples) <= 1 {
		return ifTree{
			root: &ifNode{isLeaf: true, size: len(samples)},
			size: len(samples),
			heightLimit: limit,
		}
	}

	minVal, maxVal := samples[0], samples[0]
	for _, v := range samples {
		if v < minVal {
			minVal = v
		}
		if v > maxVal {
			maxVal = v
		}
	}

	if maxVal-minVal < 1e-10 {
		return ifTree{
			root: &ifNode{isLeaf: true, size: len(samples)},
			size: len(samples),
			heightLimit: limit,
		}
	}

	splitVal := minVal + rng.Float64()*(maxVal-minVal)
	var left, right []float64
	for _, v := range samples {
		if v < splitVal {
			left = append(left, v)
		} else {
			right = append(right, v)
		}
	}

	if len(left) == 0 || len(right) == 0 {
		return ifTree{
			root: &ifNode{isLeaf: true, size: len(samples)},
			size: len(samples),
			heightLimit: limit,
		}
	}

	lt := buildTreeWithRng(left, depth+1, limit, rng)
	rt := buildTreeWithRng(right, depth+1, limit, rng)

	return ifTree{
		root: &ifNode{
			left:     lt.root,
			right:    rt.root,
			splitVal: splitVal,
			isLeaf:   false,
			size:     len(samples),
		},
		size: len(samples),
		heightLimit: limit,
	}
}

package predictor

import (
	"fmt"
	"math"
	"sync"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

type Predictor struct {
	ewma       *EWMAModel
	zscore     *ZScoreModel
	roc        *RateOfChange
	scorer     *Scorer
	config     ScorerConfig
	mu         sync.RWMutex
	datapoints map[string]int
	patterns   []PatternMatcher
}

func NewPredictor(config ScorerConfig) *Predictor {
	return &Predictor{
		ewma:       NewEWMAModel(DefaultEWMAAlpha),
		zscore:     NewZScoreModel(DefaultZScoreWindow),
		roc:        &RateOfChange{},
		scorer:     NewScorer(config),
		config:     config,
		datapoints: make(map[string]int),
	}
}

func (p *Predictor) AddPattern(m PatternMatcher) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.patterns = append(p.patterns, m)
}

func (p *Predictor) RunPatterns(metrics []MetricResult, events []models.AnomalyRecord, resources ResourceSnapshot) []models.PredictionResult {
	p.mu.RLock()
	patterns := make([]PatternMatcher, len(p.patterns))
	copy(patterns, p.patterns)
	p.mu.RUnlock()

	var results []models.PredictionResult
	for _, pat := range patterns {
		matches := pat.Match(metrics, events, resources)
		for _, m := range matches {
			results = append(results, patternToResult(m))
		}
	}
	return results
}

func (p *Predictor) Run(metrics []MetricResult) ([]models.PredictionResult, error) {
	if len(metrics) == 0 {
		return nil, fmt.Errorf("no metrics to analyze")
	}

	var results []models.PredictionResult

	for _, mr := range metrics {
		if len(mr.Values) == 0 {
			continue
		}

		for _, pt := range mr.Values {
			key := metricKey(mr.Name, pt.Labels)

			p.mu.Lock()
			p.datapoints[key]++
			dp := p.datapoints[key]
			p.mu.Unlock()

			if dp < minimumWarmupPoints {
				p.ewma.Update(key, pt.Value)
				p.zscore.AddValue(key, pt.Value)
				continue
			}

			_, deviation := p.ewma.Update(key, pt.Value)

			p.zscore.AddValue(key, pt.Value)
			zScore := p.zscore.Score(key, pt.Value)

			ewmaScore := 0.0
			if pt.Value != 0 {
				relDev := math.Abs(deviation) / math.Abs(pt.Value)
				ewmaScore = math.Min(relDev, 1.0)
			}

			zNorm := math.Min(math.Abs(zScore)/3.0, 1.0)

			rocScore := 0.0
			if dp >= 4 {
				relDev := 0.0
				if pt.Value != 0 {
					relDev = math.Abs(deviation) / math.Abs(pt.Value)
				}
				rocScore = math.Min(relDev, 1.0)
			}

			score := p.scorer.Combine(ewmaScore, zNorm, rocScore)

			if score >= 0.3 {
				results = append(results, models.PredictionResult{
					Type:      "statistical",
					Entity:    entityName(mr.Name, pt.Labels),
					Namespace: pt.Labels["namespace"],
					Score:     score,
					Timestamp: time.Now(),
				})
			}
		}
	}

	return results, nil
}

func metricKey(name string, labels map[string]string) string {
	if len(labels) == 0 {
		return name
	}
	key := name
	for _, k := range []string{"pod", "container", "namespace", "node", "instance"} {
		if v, ok := labels[k]; ok {
			key += "/" + v
		}
	}
	return key
}

func entityName(metricName string, labels map[string]string) string {
	for _, k := range []string{"pod", "node", "container", "instance"} {
		if v, ok := labels[k]; ok {
			return v
		}
	}
	return metricName
}

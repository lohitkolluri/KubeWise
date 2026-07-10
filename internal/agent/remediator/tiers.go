package remediator

import (
	"fmt"
	"sync"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func (c *Correlator) gateByTier(tier models.RiskTier, plan models.RemediationPlan) string {
	switch tier {
	case models.RiskTier1:
		return ""
	case models.RiskTier2:
		if !c.tierAssigner.CheckCooldown(plan.Action.Namespace, plan.Action.Type) {
			return fmt.Sprintf("cooldown active for %s/%s", plan.Action.Namespace, plan.Action.Type)
		}
		return ""
	case models.RiskTier3:
		if plan.Action.Type == "escalate" {
			return ""
		}
		return "" // queued for human approval after tier gate
	case models.RiskTier4:
		// T4 should never auto-execute, but it also shouldn't be auto-rejected
		// when the action type is known. Instead, require explicit operator approval.
		if !knownActionTypes[plan.Action.Type] {
			return fmt.Sprintf("T4 action rejected (unknown action): %s %s/%s (blast radius: %s)",
				plan.Action.Type, plan.Action.Namespace, plan.Action.Target, plan.Risk.BlastRadius)
		}
		return ""
	default:
		return fmt.Sprintf("unknown tier: %s", tier)
	}
}

// ActionTierMap defines the default risk tier for each action type.
var ActionTierMap = map[string]models.RiskTier{
	"restart_pod":         models.RiskTier1,
	"delete_pod":          models.RiskTier1,
	"scale_replicas":      models.RiskTier2,
	"rollback_deployment": models.RiskTier2,
	"patch_resources":     models.RiskTier2,
	"view_logs":           models.RiskTier1,
	"escalate":            models.RiskTier3,
	"noop":                models.RiskTier1,
}

// BlastRadiusTier maps blast radius to a promotion level (number of tiers to increase).
var BlastRadiusTier = map[string]int{
	"single_pod":    0,
	"multiple_pods": 1,
	"service":       1,
	"cluster":       3, // always promote to T4
}

// TierAssigner determines the risk tier for a remediation plan.
type TierAssigner struct {
	mu          sync.Mutex
	cooldowns   map[string]time.Time
	cooldownDur time.Duration
}

// NewTierAssigner creates a tier assigner with the given cooldown duration.
func NewTierAssigner(cooldown time.Duration) *TierAssigner {
	if cooldown <= 0 {
		cooldown = 5 * time.Minute
	}
	return &TierAssigner{
		cooldowns:   make(map[string]time.Time),
		cooldownDur: cooldown,
	}
}

// AssignTier determines the effective risk tier for a plan.
func (ta *TierAssigner) AssignTier(plan models.RemediationPlan) models.RiskTier {
	// Base tier from action type
	baseTier, ok := ActionTierMap[plan.Action.Type]
	if !ok {
		return models.RiskTier4 // Unknown actions are always rejected
	}

	// noop and escalate stay at their base tier
	if plan.Action.Type == "noop" || plan.Action.Type == "escalate" {
		return baseTier
	}

	// Promote based on blast radius
	promotion := BlastRadiusTier[plan.Risk.BlastRadius]
	tierVal := tierToInt(baseTier) + promotion
	if tierVal >= 4 {
		return models.RiskTier4
	}
	return intToTier(tierVal)
}

// CheckCooldown returns true if the given (namespace, action) is not in cooldown.
func (ta *TierAssigner) CheckCooldown(namespace, action string) bool {
	ta.mu.Lock()
	defer ta.mu.Unlock()

	key := cooldownKey(namespace, action)
	if until, exists := ta.cooldowns[key]; exists && time.Now().Before(until) {
		return false
	}
	if until, exists := ta.cooldowns[key]; exists && time.Now().After(until) {
		delete(ta.cooldowns, key)
	}
	return true
}

// SetCooldown sets the cooldown for a (namespace, action) pair.
func (ta *TierAssigner) SetCooldown(namespace, action string) {
	ta.mu.Lock()
	defer ta.mu.Unlock()

	key := cooldownKey(namespace, action)
	ta.cooldowns[key] = time.Now().Add(ta.cooldownDur)
}

// InCooldown returns true if the action is currently in cooldown.
func (ta *TierAssigner) InCooldown(namespace, action string) bool {
	return !ta.CheckCooldown(namespace, action)
}

func cooldownKey(namespace, action string) string {
	return fmt.Sprintf("%s/%s", namespace, action)
}

func tierToInt(t models.RiskTier) int {
	switch t {
	case models.RiskTier1:
		return 1
	case models.RiskTier2:
		return 2
	case models.RiskTier3:
		return 3
	case models.RiskTier4:
		return 4
	default:
		return 4
	}
}

func intToTier(i int) models.RiskTier {
	switch {
	case i <= 1:
		return models.RiskTier1
	case i == 2:
		return models.RiskTier2
	case i == 3:
		return models.RiskTier3
	default:
		return models.RiskTier4
	}
}

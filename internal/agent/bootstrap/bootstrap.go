package bootstrap

import (
	"context"
	"fmt"
	"log"
	"os"
	"strings"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/lohitkolluri/KubeWise/internal/agent/llm"
	"github.com/lohitkolluri/KubeWise/internal/agent/remediator"
	"github.com/lohitkolluri/KubeWise/internal/agent/store"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// Runtime holds bootstrapped agent dependencies loaded from env + store.
type Runtime struct {
	Store           *store.Store
	Config          *models.AgentConfig
	Interval        time.Duration
	LLMConfig       llm.Config
	Remediation     remediator.RemediationConfig
	APIAddr         string
	ForecasterAddr  string
	DataDir         string
}

// Init opens the store, loads or seeds config, and validates runtime settings.
func Init() (*Runtime, error) {
	dataDir := os.Getenv("KUBEWISE_DATA_DIR")
	if dataDir == "" {
		dataDir = "/tmp/kubewise"
	}
	if err := os.MkdirAll(dataDir, 0o700); err != nil {
		return nil, fmt.Errorf("create data dir: %w", err)
	}

	s, err := store.Open(dataDir + "/agent.db")
	if err != nil {
		return nil, fmt.Errorf("open store: %w", err)
	}

	cfg := defaultConfig()
	configPath := os.Getenv("KUBEWISE_CONFIG_PATH")
	if configPath != "" {
		existing, err := s.LoadConfig()
		if err != nil {
			s.Close()
			return nil, fmt.Errorf("load config from store: %w", err)
		}
		if existing == nil {
			cfg, err = loadConfigFile(configPath)
			if err != nil {
				s.Close()
				return nil, fmt.Errorf("seed config: %w", err)
			}
			if err := s.SaveConfig(cfg); err != nil {
				s.Close()
				return nil, fmt.Errorf("save seeded config: %w", err)
			}
			log.Printf("seeded config from %s", configPath)
		} else {
			cfg = existing
		}
	} else {
		existing, err := s.LoadConfig()
		if err == nil && existing != nil {
			cfg = existing
		} else if err == nil && existing == nil {
			if err := s.SaveConfig(cfg); err != nil {
				s.Close()
				return nil, fmt.Errorf("save default config: %w", err)
			}
		}
	}

	normalizeConfig(cfg)

	interval, err := time.ParseDuration(cfg.ScrapeInterval)
	if err != nil {
		interval = 30 * time.Second
	}

	remCfg := buildRemediationConfig(cfg)
	llmCfg := buildLLMConfig(cfg)
	validateLLM(llmCfg)

	if err := validateAPIAuth(); err != nil {
		s.Close()
		return nil, err
	}

	apiAddr := os.Getenv("KUBEWISE_ADDR")
	if apiAddr == "" {
		apiAddr = ":8080"
	}

	return &Runtime{
		Store:          s,
		Config:         cfg,
		Interval:       interval,
		LLMConfig:      llmCfg,
		Remediation:    remCfg,
		APIAddr:        apiAddr,
		ForecasterAddr: os.Getenv("FORECASTER_ADDR"),
		DataDir:        dataDir,
	}, nil
}

func defaultConfig() *models.AgentConfig {
	return &models.AgentConfig{
		ScrapeInterval:    "30s",
		PrometheusAddress: "http://localhost:9090",
		LLMProvider:       "openrouter",
		LLMModel:          llm.DefaultModel,
		Remediation: models.RemediationConfig{
			Mode:   "dry-run",
			DryRun: true,
		},
	}
}

func loadConfigFile(path string) (*models.AgentConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config file: %w", err)
	}
	var cfg models.AgentConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse config: %w", err)
	}
	normalizeConfig(&cfg)
	return &cfg, nil
}

func normalizeConfig(cfg *models.AgentConfig) {
	if cfg.ScrapeInterval == "" {
		cfg.ScrapeInterval = "30s"
	}
	if cfg.PrometheusAddress == "" {
		cfg.PrometheusAddress = "http://localhost:9090"
	}
	if cfg.LLMProvider == "" {
		cfg.LLMProvider = "openrouter"
	}
	if cfg.LLMModel == "" {
		cfg.LLMModel = llm.DefaultModel
	}
	if cfg.Remediation.Mode == "" {
		cfg.Remediation.Mode = "dry-run"
	}
}

func buildRemediationConfig(cfg *models.AgentConfig) remediator.RemediationConfig {
	minConf := cfg.Remediation.MinConfidence
	if minConf <= 0 {
		minConf = 0.7
	}
	switch cfg.Remediation.Mode {
	case models.RemediationModeDryRun, models.RemediationModeAuto, models.RemediationModeOff, models.RemediationModeSemi, "":
		if cfg.Remediation.Mode == models.RemediationModeSemi || cfg.Remediation.Mode == models.RemediationModeDryRun || cfg.Remediation.Mode == "" {
			cfg.Remediation.DryRun = true
		}
	default:
		if !models.ValidRemediationMode(cfg.Remediation.Mode) {
			log.Printf("agent: unknown remediation mode %q, defaulting to dry-run", cfg.Remediation.Mode)
			cfg.Remediation.Mode = models.RemediationModeDryRun
			cfg.Remediation.DryRun = true
		}
	}
	remCfg := remediator.RemediationConfig{
		Mode:            cfg.Remediation.Mode,
		DryRun:          cfg.Remediation.DryRun || cfg.Remediation.Mode == models.RemediationModeDryRun,
		Allowlist:       cfg.Remediation.Allowlist,
		Denylist:        cfg.Remediation.NamespaceDenylist,
		MinConfidence:   minConf,
		RateLimit:       cfg.Remediation.RateLimit,
		WatchNamespaces: cfg.WatchNamespaces,
	}
	if remCfg.RateLimit <= 0 {
		remCfg.RateLimit = 10
	}
	return remCfg
}

func buildLLMConfig(cfg *models.AgentConfig) llm.Config {
	apiKey := strings.TrimSpace(os.Getenv("OPENROUTER_API_KEY"))
	llmBaseURL := strings.TrimSpace(os.Getenv("OLLAMA_BASE_URL"))
	if cfg.LLMBaseURL != "" {
		llmBaseURL = cfg.LLMBaseURL
	}
	llmCfg := llm.Config{
		Provider: cfg.LLMProvider,
		APIKey:   apiKey,
		Model:    cfg.LLMModel,
		BaseURL:  llmBaseURL,
	}
	if llmCfg.Provider == "" {
		llmCfg.Provider = "openrouter"
	}
	return llmCfg
}

func validateLLM(llmCfg llm.Config) {
	if llmCfg.Provider == "openrouter" && llmCfg.APIKey != "" {
		checkCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		client, err := llm.NewClient(llmCfg)
		if err != nil {
			log.Printf("agent: warning: llm client: %v", err)
			return
		}
		if err := client.ValidateKey(checkCtx); err != nil {
			log.Printf("agent: warning: LLM key validation failed: %v", err)
		} else {
			log.Printf("agent: LLM provider %s validated", client.ProviderName())
		}
		return
	}
	if llmCfg.Provider == "ollama" {
		checkCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		client, err := llm.NewClient(llmCfg)
		if err != nil {
			log.Printf("agent: warning: ollama client: %v", err)
			return
		}
		if err := client.ValidateKey(checkCtx); err != nil {
			log.Printf("agent: warning: ollama unreachable: %v", err)
		} else {
			log.Printf("agent: ollama at %s ready", llmCfg.BaseURL)
		}
	}
}

func validateAPIAuth() error {
	if strings.TrimSpace(os.Getenv("KUBEWISE_API_TOKEN")) == "" {
		log.Printf("agent: WARNING: KUBEWISE_API_TOKEN is not set — agent HTTP API is unauthenticated")
	}
	if os.Getenv("KUBEWISE_REQUIRE_API_TOKEN") == "true" && strings.TrimSpace(os.Getenv("KUBEWISE_API_TOKEN")) == "" {
		return fmt.Errorf("KUBEWISE_REQUIRE_API_TOKEN is set but KUBEWISE_API_TOKEN is empty — refusing to start")
	}
	return nil
}

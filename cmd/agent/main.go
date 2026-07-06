package main

import (
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/lohitkolluri/KubeWise/internal/agent"
	"github.com/lohitkolluri/KubeWise/internal/agent/remediator"
	"github.com/lohitkolluri/KubeWise/internal/agent/store"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func loadConfigFile(path string) (*models.AgentConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config file: %w", err)
	}
	var cfg models.AgentConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse config: %w", err)
	}
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
		cfg.LLMModel = "openrouter/free"
	}
	if cfg.Remediation.Mode == "" {
		cfg.Remediation.Mode = "dry-run"
	}
	return &cfg, nil
}

func main() {
	dataDir := os.Getenv("KUBEWISE_DATA_DIR")
	if dataDir == "" {
		dataDir = "/tmp/kubewise"
	}
	if err := os.MkdirAll(dataDir, 0700); err != nil {
		fmt.Fprintf(os.Stderr, "create data dir: %v\n", err)
		os.Exit(1)
	}

	s, err := store.Open(dataDir + "/agent.db")
	if err != nil {
		fmt.Fprintf(os.Stderr, "open store: %v\n", err)
		os.Exit(1)
	}
	defer s.Close()

	cfg := &models.AgentConfig{
		ScrapeInterval:    "30s",
		PrometheusAddress: "http://localhost:9090",
		LLMProvider:       "openrouter",
		LLMModel:          "openrouter/free",
		Remediation: models.RemediationConfig{
			Mode:   "dry-run",
			DryRun: true,
		},
	}

	// Seed config from the mounted ConfigMap on first boot.
	configPath := os.Getenv("KUBEWISE_CONFIG_PATH")
	if configPath != "" {
		existing, err := s.LoadConfig()
		if err != nil {
			log.Fatalf("load config from store: %v", err)
		}
		if existing == nil {
			cfg, err = loadConfigFile(configPath)
			if err != nil {
				log.Fatalf("seed config: %v", err)
			}
			if err := s.SaveConfig(cfg); err != nil {
				log.Fatalf("save seeded config: %v", err)
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
				log.Fatalf("save default config: %v", err)
			}
		}
	}

	interval, err := time.ParseDuration(cfg.ScrapeInterval)
	if err != nil {
		interval = 30 * time.Second
	}

	// Build remediation config from the agent config
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
		Mode:          cfg.Remediation.Mode,
		DryRun:        cfg.Remediation.DryRun || cfg.Remediation.Mode == models.RemediationModeDryRun,
		Allowlist:     cfg.Remediation.Allowlist,
		Denylist:      cfg.Remediation.NamespaceDenylist,
		MinConfidence: minConf,
		RateLimit:     cfg.Remediation.RateLimit,
	}
	if remCfg.RateLimit <= 0 {
		remCfg.RateLimit = 10
	}

	apiAddr := os.Getenv("KUBEWISE_ADDR")
	if apiAddr == "" {
		apiAddr = ":8080"
	}

	// Get API key from env
	apiKey := os.Getenv("OPENROUTER_API_KEY")

	// Optional Tier-2 forecasting sidecar address
	forecasterAddr := os.Getenv("FORECASTER_ADDR")

	// Create the agent (wraps collector, predictor, LLM, remediation, API server)
	agt, err := agent.NewAgent(s, cfg, interval, apiKey, cfg.LLMModel, remCfg, forecasterAddr, apiAddr)
	if err != nil {
		fmt.Fprintf(os.Stderr, "create agent: %v\n", err)
		os.Exit(1)
	}

	// Run the agent (blocks until signal)
	go func() {
		quit := make(chan os.Signal, 1)
		signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
		<-quit
		log.Println("shutting down...")
		agt.Stop()
	}()

	if err := agt.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "agent error: %v\n", err)
		os.Exit(1)
	}
}

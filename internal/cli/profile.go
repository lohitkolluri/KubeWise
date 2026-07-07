package cli

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	yaml "gopkg.in/yaml.v3"
)

// CLIProfile holds kwctl client settings (not the agent runtime config).
type CLIProfile struct {
	AgentURL       string `yaml:"agent_url,omitempty"`
	AgentNamespace string `yaml:"agent_namespace,omitempty"`
	AgentService   string `yaml:"agent_service,omitempty"`
	APIToken       string `yaml:"api_token,omitempty"`
	Output         string `yaml:"output,omitempty"`
	TimeoutSeconds int    `yaml:"timeout_seconds,omitempty"`
	Kubeconfig     string `yaml:"kubeconfig,omitempty"`
	Context        string `yaml:"context,omitempty"`
}

type profileFile struct {
	Current  string                `yaml:"current_profile"`
	Profiles map[string]CLIProfile `yaml:"profiles"`
}

func profilePath() (string, error) {
	base := os.Getenv("XDG_CONFIG_HOME")
	if base == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		base = filepath.Join(home, ".config")
	}
	dir := filepath.Join(base, "kwctl")
	if err := os.MkdirAll(dir, 0700); err != nil {
		return "", err
	}
	return filepath.Join(dir, "config.yaml"), nil
}

func loadProfileFile() (*profileFile, error) {
	path, err := profilePath()
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return defaultProfileFile(), nil
		}
		return nil, err
	}
	var pf profileFile
	if err := yaml.Unmarshal(data, &pf); err != nil {
		return nil, fmt.Errorf("parse profile %s: %w", path, err)
	}
	if pf.Profiles == nil {
		pf.Profiles = map[string]CLIProfile{}
	}
	if pf.Current == "" {
		pf.Current = "default"
	}
	if _, ok := pf.Profiles[pf.Current]; !ok && len(pf.Profiles) > 0 {
		for name := range pf.Profiles {
			pf.Current = name
			break
		}
	}
	if len(pf.Profiles) == 0 {
		return defaultProfileFile(), nil
	}
	return &pf, nil
}

func defaultProfileFile() *profileFile {
	return &profileFile{
		Current: "default",
		Profiles: map[string]CLIProfile{
			"default": {
				AgentURL:       defaultAgentURL,
				AgentNamespace: "kubewise",
				AgentService:   "kubewise",
					Output:         "table",
					TimeoutSeconds: 15,
			},
		},
	}
}

func saveProfileFile(pf *profileFile) error {
	path, err := profilePath()
	if err != nil {
		return err
	}
	data, err := yaml.Marshal(pf)
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0600)
}

func activeProfile() CLIProfile {
	pf, err := loadProfileFile()
	if err != nil {
		return defaultProfileFile().Profiles["default"]
	}
	p := pf.Profiles[pf.Current]
	if p.AgentURL == "" {
		p.AgentURL = defaultAgentURL
	}
	if p.AgentNamespace == "" {
		p.AgentNamespace = "kubewise"
	}
		if p.AgentService == "" {
			p.AgentService = "kubewise"
		}
	if p.Output == "" {
		p.Output = "table"
	}
	if p.TimeoutSeconds <= 0 {
		p.TimeoutSeconds = 15
	}
	return p
}

func applyProfileDefaults() {
	// Priority order:
	// 1) explicit flags
	// 2) explicit env vars
	// 3) selected profile (or default)
	if profileName != "" {
		pf, err := loadProfileFile()
		if err == nil {
			if p, ok := pf.Profiles[profileName]; ok {
				if agentURL == "" {
					agentURL = p.AgentURL
				}
				if p.AgentNamespace != "" {
					agentNS = p.AgentNamespace
				}
				if p.AgentService != "" {
					agentSvc = p.AgentService
				}
				if p.Output != "" && outputFormat == "table" {
					outputFormat = p.Output
				}
				if httpTimeout <= 0 && p.TimeoutSeconds > 0 {
					httpTimeout = p.TimeoutSeconds
				}
				if apiToken == "" && p.APIToken != "" {
					apiToken = p.APIToken
				}
				if kubeconfig == "" && p.Kubeconfig != "" {
					kubeconfig = p.Kubeconfig
				}
				if contextName == "" && p.Context != "" {
					contextName = p.Context
				}
				return
			}
		}
	}
	p := activeProfile()
	if agentURL == "" {
		if u := os.Getenv("KUBEWISE_AGENT_URL"); u != "" {
			agentURL = u
		} else {
			agentURL = p.AgentURL
		}
	}
	if agentNS == "" || agentNS == "kubewise" {
		if p.AgentNamespace != "" {
			agentNS = p.AgentNamespace
		}
	}
	if agentSvc == "" || agentSvc == "kubewise" {
		if p.AgentService != "" {
			agentSvc = p.AgentService
		}
	}
	// The root flag default is "table". Allow profile to override only when the
	// user didn't explicitly choose another output format.
	if outputFormat == "table" && p.Output != "" {
		outputFormat = p.Output
	}
	if kubeconfig == "" && p.Kubeconfig != "" {
		kubeconfig = p.Kubeconfig
	}
	if contextName == "" && p.Context != "" {
		contextName = p.Context
	}
	if httpTimeout <= 0 {
		httpTimeout = p.TimeoutSeconds
	}
	if apiToken == "" {
		if t := os.Getenv("KUBEWISE_API_TOKEN"); t != "" {
			apiToken = t
		} else {
			apiToken = p.APIToken
		}
	}
}

func setProfileField(name, key, value string) error {
	pf, err := loadProfileFile()
	if err != nil {
		return err
	}
	if name == "" {
		name = pf.Current
	}
	if pf.Profiles == nil {
		pf.Profiles = map[string]CLIProfile{}
	}
	prof := pf.Profiles[name]
	if prof.AgentURL == "" {
		prof = defaultProfileFile().Profiles["default"]
	}
	switch key {
	case "agent-url", "agent_url":
		prof.AgentURL = value
	case "agent-namespace", "agent_namespace":
		prof.AgentNamespace = value
	case "agent-service", "agent_service":
		prof.AgentService = value
	case "api-token", "api_token":
		prof.APIToken = value
	case "output":
		prof.Output = value
	case "timeout", "timeout_seconds":
		var n int
		if _, err := fmt.Sscanf(value, "%d", &n); err != nil || n <= 0 {
			return fmt.Errorf("timeout must be a positive integer")
		}
		prof.TimeoutSeconds = n
	case "kubeconfig":
		prof.Kubeconfig = value
	case "context":
		prof.Context = value
	default:
		return fmt.Errorf("unknown profile key %q", key)
	}
	pf.Profiles[name] = prof
	return saveProfileFile(pf)
}

func parseSetArg(arg string) (key, value string, err error) {
	if i := strings.Index(arg, "="); i >= 0 {
		return arg[:i], arg[i+1:], nil
	}
	return "", "", fmt.Errorf("expected key=value, got %q", arg)
}

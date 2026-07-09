package tools

import "fmt"

var kubectlPositionalArgs = map[string]bool{
	"resource":  true,
	"name":      true,
	"namespace": true,
}

var blockedKubectlFlags = map[string]bool{
	"server":                   true,
	"kubeconfig":               true,
	"token":                    true,
	"as":                       true,
	"as-group":                 true,
	"cluster":                  true,
	"context":                  true,
	"user":                     true,
	"certificate-authority":    true,
	"client-certificate":       true,
	"client-key":               true,
	"insecure-skip-tls-verify": true,
	"tls-server-name":          true,
	"proxy":                    true,
	"request-timeout":          true,
}

var kubectlAllowedFlagsByCommand = map[string]map[string]bool{
	"get": {
		"output": true, "o": true, "selector": true, "label-selector": true,
		"field-selector": true, "watch": true, "show-labels": true, "sort-by": true, "limit": true,
	},
	"describe": {},
	"logs": {
		"tail": true, "since": true, "timestamps": true, "container": true,
		"previous": true, "follow": true, "limit-bytes": true,
	},
	"top":      {},
	"events":   {},
	"explain":  {"recursive": true, "api-version": true},
	"apply":    {"filename": true, "f": true, "force": true, "dry-run": true, "server-side": true, "validate": true},
	"delete":   {"force": true, "grace-period": true, "wait": true},
	"rollout":  {"command": true, "timeout": true, "wait": true},
	"scale":    {"replicas": true, "current-replicas": true},
	"label":    {"overwrite": true, "labels": true},
	"annotate": {"overwrite": true},
	"drain": {
		"ignore-daemonsets": true, "delete-emptydir-data": true, "force": true,
		"grace-period": true, "timeout": true,
	},
	"cordon":   {},
	"uncordon": {},
	"taint":    {},
}

var helmPositionalArgs = map[string]bool{
	"namespace": true,
	"release":   true,
	"chart":     true,
	"revision":  true,
}

var blockedHelmFlags = map[string]bool{
	"kubeconfig":               true,
	"kube-context":             true,
	"password":                 true,
	"username":                 true,
	"ca-file":                  true,
	"cert-file":                true,
	"key-file":                 true,
	"insecure-skip-tls-verify": true,
	"repository-config":        true,
	"registry-config":          true,
}

var helmAllowedFlagsByCommand = map[string]map[string]bool{
	"list":         {"output": true, "filter": true, "all": true, "max": true, "offset": true},
	"status":       {"revision": true},
	"history":      {"max": true, "offset": true},
	"upgrade":      {"version": true, "values": true, "set": true, "reuse-values": true, "install": true, "atomic": true, "timeout": true, "dry-run": true, "wait": true},
	"rollback":     {"wait": true, "timeout": true, "cleanup-on-fail": true},
	"uninstall":    {"keep-history": true, "timeout": true, "wait": true},
	"get":          {"revision": true},
	"get values":   {"revision": true},
	"get manifest": {"revision": true},
	"get notes":    {"revision": true},
	"get hooks":    {"revision": true},
	"show":         {"version": true},
	"show chart":   {"version": true},
	"show values":  {"version": true},
	"show all":     {"version": true},
}

func validateToolArgKeys(args map[string]string, positional map[string]bool, allowed map[string]bool, blocked map[string]bool) error {
	for key := range args {
		if positional[key] {
			continue
		}
		if blocked[key] {
			return fmt.Errorf("flag %q is not permitted", key)
		}
		if allowed == nil {
			return fmt.Errorf("flag %q is not allowed for this command", key)
		}
		if !allowed[key] {
			return fmt.Errorf("flag %q is not allowed for this command", key)
		}
	}
	return nil
}

func appendAllowedToolFlags(args []string, actionArgs map[string]string, positional map[string]bool, allowed map[string]bool) []string {
	for key, value := range actionArgs {
		if positional[key] || value == "" {
			continue
		}
		if allowed != nil && !allowed[key] {
			continue
		}
		args = append(args, "--"+key, value)
	}
	return args
}

func kubectlAllowedFlags(cmd string) map[string]bool {
	if flags, ok := kubectlAllowedFlagsByCommand[cmd]; ok {
		return flags
	}
	return nil
}

func helmAllowedFlags(cmd string) map[string]bool {
	if flags, ok := helmAllowedFlagsByCommand[cmd]; ok {
		return flags
	}
	return nil
}

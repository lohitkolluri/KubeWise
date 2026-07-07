package llm

import (
	"context"
	"errors"
	"testing"
)

func TestModelChain(t *testing.T) {
	chain := modelChain(DefaultModel)
	if chain[0] != DefaultModel {
		t.Fatalf("primary: got %s", chain[0])
	}
	if len(chain) < 1 {
		t.Fatalf("expected non-empty chain, got %d: %v", len(chain), chain)
	}
}

func TestIsFreeModel(t *testing.T) {
	if !IsFreeModel(FreeModelGPTOSS) {
		t.Fatal("gpt-oss free")
	}
	if IsFreeModel(DefaultModel) {
		t.Fatal("default model must not be free")
	}
}

func TestModelChain_CustomPrimary(t *testing.T) {
	chain := modelChain("custom/model")
	if chain[0] != "custom/model" {
		t.Fatal("custom primary not first")
	}
	for _, m := range chain {
		if m == "" {
			t.Fatal("empty model in chain")
		}
	}
}

func TestIsRetryableOpenRouterError(t *testing.T) {
	if !isRetryableOpenRouterError(errors.New("chat completion: context deadline exceeded")) {
		t.Fatal("expected timeout retryable")
	}
	if isRetryableOpenRouterError(context.Canceled) {
		t.Fatal("canceled should not retry")
	}
	if isRetryableOpenRouterError(errors.New("openrouter auth failed")) {
		t.Fatal("auth error should not retry")
	}
}

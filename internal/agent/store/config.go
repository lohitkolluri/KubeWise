package store

import (
	"encoding/json"
	"errors"
	"fmt"

	bolt "go.etcd.io/bbolt"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

const configKey = "agent_config"

// SaveConfig persists the agent configuration.
func (s *Store) SaveConfig(cfg *models.AgentConfig) error {
	if cfg == nil {
		return errors.New("config must not be nil")
	}
	data, err := json.Marshal(cfg)
	if err != nil {
		return fmt.Errorf("marshal config: %w", err)
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		return tx.Bucket(bucketConfig).Put([]byte(configKey), data)
	})
}

// LoadConfig retrieves the agent configuration.
// Returns nil (no error) if no config has been saved yet.
func (s *Store) LoadConfig() (*models.AgentConfig, error) {
	var cfg *models.AgentConfig
	err := s.db.View(func(tx *bolt.Tx) error {
		data := tx.Bucket(bucketConfig).Get([]byte(configKey))
		if data == nil {
			return nil
		}
		var c models.AgentConfig
		if err := json.Unmarshal(data, &c); err != nil {
			return fmt.Errorf("unmarshal config: %w", err)
		}
		cfg = &c
		return nil
	})
	return cfg, err
}

package main

import (
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/lohitkolluri/KubeWise/internal/agent"
	"github.com/lohitkolluri/KubeWise/internal/agent/bootstrap"
)

func main() {
	rt, err := bootstrap.Init()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer rt.Store.Close()

	agt, err := agent.NewAgent(
		rt.Store,
		rt.Config,
		rt.Interval,
		rt.LLMConfig,
		rt.Remediation,
		rt.ForecasterAddr,
		rt.APIAddr,
	)
	if err != nil {
		fmt.Fprintf(os.Stderr, "create agent: %v\n", err)
		os.Exit(1)
	}

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

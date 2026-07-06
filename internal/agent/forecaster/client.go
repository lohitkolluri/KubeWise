// Package forecaster provides a Go gRPC client for the Python forecasting
// sidecar. It wraps the generated protobuf client with connection management,
// timeouts, and reconnection.
package forecaster

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/lohitkolluri/KubeWise/internal/agent/forecaster/pb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// DefaultAddress is the default sidecar gRPC address.
const DefaultAddress = "localhost:50051"

// DefaultTimeout is the max wait for a Forecast RPC.
const DefaultTimeout = 30 * time.Second

// Client wraps the gRPC connection and generated stub.
type Client struct {
	conn    *grpc.ClientConn
	stub    pb.ForecasterClient
	address string
	timeout time.Duration
}

// ForecastRequest mirrors the proto message for ergonomic Go usage.
type ForecastRequest struct {
	MetricName      string
	Values          []float64
	Timestamps      []float64
	Labels          map[string]string
	Horizon         int
	IntervalSeconds float64
}

// ForecastPoint is a single predicted data point.
type ForecastPoint struct {
	Timestamp   float64
	Value       float64
	LowerBound  float64
	UpperBound  float64
}

// ForecastResponse contains the sidecar's prediction output.
type ForecastResponse struct {
	Points       []ForecastPoint
	Status       string
	ErrorMessage string
}

// NewClient connects to the sidecar at address (default localhost:50051).
func NewClient(address string, timeout time.Duration) (*Client, error) {
	if address == "" {
		address = DefaultAddress
	}
	if timeout <= 0 {
		timeout = DefaultTimeout
	}

	conn, err := grpc.NewClient(address,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(10*1024*1024)),
	)
	if err != nil {
		return nil, fmt.Errorf("forecaster grpc dial: %w", err)
	}

	return &Client{
		conn:    conn,
		stub:    pb.NewForecasterClient(conn),
		address: address,
		timeout: timeout,
	}, nil
}

// Forecast sends metric history to the Python sidecar and returns predictions.
func (c *Client) Forecast(ctx context.Context, req *ForecastRequest) (*ForecastResponse, error) {
	if req == nil {
		return nil, fmt.Errorf("forecast request is nil")
	}
	if len(req.Values) != len(req.Timestamps) {
		return nil, fmt.Errorf("values and timestamps length mismatch: %d vs %d", len(req.Values), len(req.Timestamps))
	}
	if len(req.Values) == 0 {
		return nil, fmt.Errorf("forecast request has no values")
	}

	ctx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()

	pbReq := &pb.ForecastRequest{
		MetricName:      req.MetricName,
		Values:          req.Values,
		Timestamps:      req.Timestamps,
		Labels:          req.Labels,
		Horizon:         int32(req.Horizon),
		IntervalSeconds: req.IntervalSeconds,
	}

	pbResp, err := c.stub.Forecast(ctx, pbReq)
	if err != nil {
		return nil, fmt.Errorf("forecaster rpc: %w", err)
	}

	resp := &ForecastResponse{
		Status:       pbResp.Status,
		ErrorMessage: pbResp.ErrorMessage,
		Points:       make([]ForecastPoint, 0, len(pbResp.Points)),
	}
	for _, p := range pbResp.Points {
		resp.Points = append(resp.Points, ForecastPoint{
			Timestamp:  p.Timestamp,
			Value:      p.Value,
			LowerBound: p.LowerBound,
			UpperBound: p.UpperBound,
		})
	}
	if resp.Status != "" && resp.Status != "ok" {
		return resp, fmt.Errorf("forecaster error: %s", resp.ErrorMessage)
	}
	return resp, nil
}

// Close tears down the gRPC connection.
func (c *Client) Close() error {
	if c.conn != nil {
		return c.conn.Close()
	}
	return nil
}

// HealthCheck pings the sidecar by sending a minimal Forecast with horizon=1.
// Returns nil if the sidecar responds within the timeout.
func (c *Client) HealthCheck(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	resp, err := c.Forecast(ctx, &ForecastRequest{
		Values:     []float64{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12},
		Timestamps: []float64{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12},
		Horizon:    1,
	})
	if err != nil {
		return fmt.Errorf("forecaster healthcheck: %w", err)
	}
	if resp.Status != "ok" {
		return fmt.Errorf("forecaster healthcheck: %s", resp.ErrorMessage)
	}
	log.Printf("forecaster: sidecar at %s is healthy", c.address)
	return nil
}

import axios from 'axios';

// Create axios instance with default config
const api = axios.create({
  baseURL: process.env.NEXT_PUBLIC_API_URL || 'http://localhost:5000',
  headers: {
    'Content-Type': 'application/json',
  },
});

// Types for API responses
export interface ClusterHealth {
  status: 'healthy' | 'degraded' | 'critical';
  prometheus: boolean;
  clusterHealth: string;
  agentAutoRemediation: boolean;
  nodes: {
    total: number;
    ready: number;
  };
  pods: {
    total: number;
    running: number;
    pending: number;
    failed: number;
    crashLoopBackOff: number;
  };
  cpuUsagePercent: number;
  memoryUsagePercent: number;
}

export interface ClusterMetrics {
  cpuPercent: number[];
  memoryPercent: number[];
  diskPercent: number[];
  nodeCount: {
    total: number;
    ready: number;
    notReady: number;
  };
  pods: {
    total: number;
    running: number;
    pending: number;
    failed: number;
    crashLoopBackOff: number;
  };
  networkReceive: number[];
  networkTransmit: number[];
  networkTotal: number;
  timeSeriesData?: {
    cpu: { timestamp: string; value: number }[];
    memory: { timestamp: string; value: number }[];
    network: { timestamp: string; value: number }[];
  };
}

export interface RemediationEvent {
  id: string;
  time: string;
  action: string;
  reason: string;
  initiatedBy: 'auto' | 'manual';
  status: 'success' | 'failed' | 'pending';
  error?: string;
}

export interface RemediationAction {
  id: string;
  label: string;
  action: string;
  target: string;
  description: string;
  parameters?: Record<string, any>;
}

export interface AppSettings {
  autoRemediation: boolean;
  alertsEnabled: boolean;
  prometheusEndpoint: string;
  refreshInterval: number;
  apiEndpoint: string;
  aiEnabled: boolean;
  selectedModel: string;
  confidence: number;
  maxRemediation: number;
}

// API service methods
const apiService = {
  // Health endpoints
  getClusterHealth: async (): Promise<ClusterHealth> => {
    try {
      const response = await api.get('/api/health');
      return response.data;
    } catch (error) {
      console.error('Error fetching cluster health:', error);
      // Return mock data if API is not available
      return {
        status: 'degraded',
        prometheus: false,
        clusterHealth: 'Connectivity issues',
        agentAutoRemediation: true,
        nodes: { total: 3, ready: 2 },
        pods: {
          total: 50,
          running: 42,
          pending: 3,
          failed: 3,
          crashLoopBackOff: 2
        },
        cpuUsagePercent: 65,
        memoryUsagePercent: 78
      };
    }
  },

  // Metrics endpoints
  getClusterMetrics: async (timeRange: string = '1h'): Promise<ClusterMetrics> => {
    try {
      const response = await api.get(`/api/metrics?timeRange=${timeRange}`);
      return response.data;
    } catch (error) {
      console.error('Error fetching cluster metrics:', error);
      // Generate some mock time series data
      const mockTimeSeriesData = {
        cpu: Array.from({ length: 24 }, (_, i) => ({
          timestamp: new Date(Date.now() - (23 - i) * 15 * 60 * 1000).toISOString(),
          value: 30 + Math.random() * 40
        })),
        memory: Array.from({ length: 24 }, (_, i) => ({
          timestamp: new Date(Date.now() - (23 - i) * 15 * 60 * 1000).toISOString(),
          value: 50 + Math.random() * 30
        })),
        network: Array.from({ length: 24 }, (_, i) => ({
          timestamp: new Date(Date.now() - (23 - i) * 15 * 60 * 1000).toISOString(),
          value: Math.random() * 100
        }))
      };

      // Return mock data if API is not available
      return {
        cpuPercent: [65, 70, 75, 68, 72],
        memoryPercent: [78, 80, 82, 79, 81],
        diskPercent: [45, 46, 47, 45, 46],
        nodeCount: { total: 3, ready: 2, notReady: 1 },
        pods: {
          total: 50,
          running: 42,
          pending: 3,
          failed: 3,
          crashLoopBackOff: 2
        },
        networkReceive: [120, 125, 118, 130, 122],
        networkTransmit: [85, 90, 88, 95, 92],
        networkTotal: 212,
        timeSeriesData: mockTimeSeriesData
      };
    }
  },

  // Remediation endpoints
  getRemediationHistory: async (): Promise<RemediationEvent[]> => {
    try {
      const response = await api.get('/api/remediation/history');
      return response.data;
    } catch (error) {
      console.error('Error fetching remediation history:', error);
      // Return mock data if API is not available
      return [
        {
          id: '1',
          time: new Date(Date.now() - 30 * 60 * 1000).toISOString(),
          action: 'Restarted pod inventory-5f9d4bcdf7-kl8pz',
          reason: 'CrashLoopBackOff detected',
          initiatedBy: 'auto',
          status: 'success'
        },
        {
          id: '2',
          time: new Date(Date.now() - 45 * 60 * 1000).toISOString(),
          action: 'Scaled deployment checkout to 5 replicas',
          reason: 'High CPU usage (85%)',
          initiatedBy: 'auto',
          status: 'success'
        },
        {
          id: '3',
          time: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
          action: 'Cordon node worker-3',
          reason: 'Disk pressure',
          initiatedBy: 'auto',
          status: 'failed',
          error: 'Permission denied'
        }
      ];
    }
  },

  getRemediationActions: async (): Promise<RemediationAction[]> => {
    try {
      const response = await api.get('/api/remediation/actions');
      return response.data;
    } catch (error) {
      console.error('Error fetching remediation actions:', error);
      // Return mock data if API is not available
      return [
        {
          id: '1',
          label: 'Restart Failed Pods',
          action: 'restartPod',
          target: 'all-failed',
          description: 'Restart all pods in CrashLoopBackOff or Failed state'
        },
        {
          id: '2',
          label: 'Scale Frontend',
          action: 'scaleDeployment',
          target: 'frontend',
          description: 'Scale the frontend deployment to 3 replicas',
          parameters: { replicas: 3 }
        }
      ];
    }
  },

  executeRemediationAction: async (actionId: string, parameters?: Record<string, any>): Promise<{ success: boolean; message: string }> => {
    try {
      const response = await api.post('/api/remediation/execute', { actionId, parameters });
      return response.data;
    } catch (error) {
      console.error('Error executing remediation action:', error);
      return {
        success: false,
        message: 'Failed to execute action: API not available'
      };
    }
  },

  // Settings endpoints
  getSettings: async (): Promise<AppSettings> => {
    try {
      const response = await api.get('/api/settings');
      return response.data;
    } catch (error) {
      console.error('Error fetching settings:', error);
      // Return mock data if API is not available
      return {
        autoRemediation: true,
        alertsEnabled: true,
        prometheusEndpoint: 'http://prometheus.monitoring:9090',
        refreshInterval: 30,
        apiEndpoint: 'http://localhost:5000',
        aiEnabled: true,
        selectedModel: 'gemini-pro',
        confidence: 0.7,
        maxRemediation: 5
      };
    }
  },

  updateSettings: async (settings: AppSettings): Promise<{ success: boolean; message: string }> => {
    try {
      const response = await api.post('/api/settings', settings);
      return response.data;
    } catch (error) {
      console.error('Error updating settings:', error);
      return {
        success: false,
        message: 'Failed to update settings: API not available'
      };
    }
  },

  testPrometheusConnection: async (endpoint: string): Promise<{ success: boolean; message: string }> => {
    try {
      const response = await api.post('/api/settings/test-prometheus', { endpoint });
      return response.data;
    } catch (error) {
      console.error('Error testing Prometheus connection:', error);
      return {
        success: false,
        message: 'Failed to connect to Prometheus: API not available'
      };
    }
  }
};

export default apiService;

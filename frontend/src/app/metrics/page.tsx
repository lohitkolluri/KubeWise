"use client";

import { useEffect, useState } from "react";
import { DashboardLayout } from "@/components/layout/dashboard-layout";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  AreaChart,
  Area,
  BarChart,
  Bar,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from "recharts";
import {
  Cpu,
  Memory,
  HardDrive,
  Activity,
  Package2,
  Network
} from "lucide-react";
import apiService, { ClusterMetrics } from "@/lib/api";
import { formatPercent } from "@/lib/utils";

export default function MetricsPage() {
  const [metrics, setMetrics] = useState<ClusterMetrics | null>(null);
  const [loading, setLoading] = useState(true);
  const [timeRange, setTimeRange] = useState("1h");

  useEffect(() => {
    const fetchMetrics = async () => {
      try {
        setLoading(true);
        const data = await apiService.getClusterMetrics(timeRange);
        setMetrics(data);
      } catch (error) {
        console.error("Failed to fetch metrics:", error);
      } finally {
        setLoading(false);
      }
    };

    fetchMetrics();

    // Set up automatic refreshing
    const intervalId = setInterval(fetchMetrics, 30000); // refresh every 30 seconds

    return () => clearInterval(intervalId);
  }, [timeRange]);

  // Create formatted data for charts
  const getTimeSeriesData = () => {
    if (!metrics?.timeSeriesData) return [];

    return metrics.timeSeriesData.cpu.map((point, index) => {
      const date = new Date(point.timestamp);
      return {
        name: `${date.getHours().toString().padStart(2, '0')}:${date.getMinutes().toString().padStart(2, '0')}`,
        cpu: point.value,
        memory: metrics.timeSeriesData?.memory[index]?.value || 0,
        disk: (metrics.diskPercent[index % metrics.diskPercent.length] || 0)
      };
    });
  };

  const getNetworkTimeSeriesData = () => {
    if (!metrics?.timeSeriesData) return [];

    return metrics.timeSeriesData.network.map((point) => {
      const date = new Date(point.timestamp);
      return {
        name: `${date.getHours().toString().padStart(2, '0')}:${date.getMinutes().toString().padStart(2, '0')}`,
        traffic: point.value
      };
    });
  };

  const getPodStatusData = () => {
    if (!metrics) return [];

    return [
      { name: "Running", value: metrics.pods.running, color: "#22c55e" },
      { name: "Pending", value: metrics.pods.pending, color: "#f59e0b" },
      { name: "Failed", value: metrics.pods.failed, color: "#ef4444" },
      { name: "CrashLoop", value: metrics.pods.crashLoopBackOff, color: "#7f1d1d" }
    ];
  };

  if (loading && !metrics) {
    return (
      <DashboardLayout>
        <div className="space-y-6">
          <div className="flex justify-between items-center">
            <h1 className="text-2xl font-bold">Metrics</h1>
            <Skeleton className="h-10 w-32" />
          </div>

          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <Card key={i}>
                <CardHeader className="pb-2">
                  <Skeleton className="h-5 w-20" />
                </CardHeader>
                <CardContent>
                  <Skeleton className="h-[200px] w-full" />
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      </DashboardLayout>
    );
  }

  return (
    <DashboardLayout>
      <div className="space-y-6">
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
          <h1 className="text-2xl font-bold">Detailed Metrics</h1>
          <Select defaultValue={timeRange} onValueChange={setTimeRange}>
            <SelectTrigger className="w-[180px]">
              <SelectValue placeholder="Select time range" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="15m">Last 15 minutes</SelectItem>
              <SelectItem value="1h">Last 1 hour</SelectItem>
              <SelectItem value="3h">Last 3 hours</SelectItem>
              <SelectItem value="24h">Last 24 hours</SelectItem>
              <SelectItem value="7d">Last 7 days</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <Tabs defaultValue="resources" className="w-full">
          <TabsList className="grid w-full grid-cols-3">
            <TabsTrigger value="resources" className="flex items-center gap-2">
              <Cpu className="h-4 w-4" />
              Resources
            </TabsTrigger>
            <TabsTrigger value="pods" className="flex items-center gap-2">
              <Package2 className="h-4 w-4" />
              Pods
            </TabsTrigger>
            <TabsTrigger value="network" className="flex items-center gap-2">
              <Network className="h-4 w-4" />
              Network
            </TabsTrigger>
          </TabsList>

          <TabsContent value="resources" className="space-y-4 mt-4">
            <div className="grid gap-4 md:grid-cols-3">
              <Card>
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                  <CardTitle className="text-sm font-medium">CPU Usage</CardTitle>
                  <Cpu className="h-4 w-4 text-muted-foreground" />
                </CardHeader>
                <CardContent>
                  <div className="text-2xl font-bold mb-4">
                    {metrics ? formatPercent(metrics.cpuPercent[metrics.cpuPercent.length - 1]) : 'Loading...'}
                  </div>
                  <div className="h-[200px]">
                    {metrics ? (
                      <ResponsiveContainer width="100%" height="100%">
                        <AreaChart
                          data={getTimeSeriesData()}
                          margin={{ top: 10, right: 10, left: 0, bottom: 0 }}
                        >
                          <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                          <XAxis dataKey="name" tick={{ fontSize: 10 }} />
                          <YAxis domain={[0, 100]} tickFormatter={(value) => `${value}%`} />
                          <Tooltip
                            formatter={(value) => [`${value}%`, 'CPU']}
                            labelFormatter={(label) => `Time: ${label}`}
                          />
                          <Area
                            type="monotone"
                            dataKey="cpu"
                            stroke="#2563eb"
                            fill="#3b82f6"
                            fillOpacity={0.5}
                          />
                        </AreaChart>
                      </ResponsiveContainer>
                    ) : (
                      <Skeleton className="h-full w-full" />
                    )}
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                  <CardTitle className="text-sm font-medium">Memory Usage</CardTitle>
                  <Memory className="h-4 w-4 text-muted-foreground" />
                </CardHeader>
                <CardContent>
                  <div className="text-2xl font-bold mb-4">
                    {metrics ? formatPercent(metrics.memoryPercent[metrics.memoryPercent.length - 1]) : 'Loading...'}
                  </div>
                  <div className="h-[200px]">
                    {metrics ? (
                      <ResponsiveContainer width="100%" height="100%">
                        <AreaChart
                          data={getTimeSeriesData()}
                          margin={{ top: 10, right: 10, left: 0, bottom: 0 }}
                        >
                          <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                          <XAxis dataKey="name" tick={{ fontSize: 10 }} />
                          <YAxis domain={[0, 100]} tickFormatter={(value) => `${value}%`} />
                          <Tooltip
                            formatter={(value) => [`${value}%`, 'Memory']}
                            labelFormatter={(label) => `Time: ${label}`}
                          />
                          <Area
                            type="monotone"
                            dataKey="memory"
                            stroke="#16a34a"
                            fill="#22c55e"
                            fillOpacity={0.5}
                          />
                        </AreaChart>
                      </ResponsiveContainer>
                    ) : (
                      <Skeleton className="h-full w-full" />
                    )}
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                  <CardTitle className="text-sm font-medium">Disk Usage</CardTitle>
                  <HardDrive className="h-4 w-4 text-muted-foreground" />
                </CardHeader>
                <CardContent>
                  <div className="text-2xl font-bold mb-4">
                    {metrics ? formatPercent(metrics.diskPercent[metrics.diskPercent.length - 1]) : 'Loading...'}
                  </div>
                  <div className="h-[200px]">
                    {metrics ? (
                      <ResponsiveContainer width="100%" height="100%">
                        <AreaChart
                          data={getTimeSeriesData()}
                          margin={{ top: 10, right: 10, left: 0, bottom: 0 }}
                        >
                          <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                          <XAxis dataKey="name" tick={{ fontSize: 10 }} />
                          <YAxis domain={[0, 100]} tickFormatter={(value) => `${value}%`} />
                          <Tooltip
                            formatter={(value) => [`${value}%`, 'Disk']}
                            labelFormatter={(label) => `Time: ${label}`}
                          />
                          <Area
                            type="monotone"
                            dataKey="disk"
                            stroke="#ca8a04"
                            fill="#eab308"
                            fillOpacity={0.5}
                          />
                        </AreaChart>
                      </ResponsiveContainer>
                    ) : (
                      <Skeleton className="h-full w-full" />
                    )}
                  </div>
                </CardContent>
              </Card>
            </div>

            <Card>
              <CardHeader>
                <CardTitle>Resource Usage Comparison</CardTitle>
                <CardDescription>
                  Comparison of CPU, memory, and disk usage over time
                </CardDescription>
              </CardHeader>
              <CardContent>
                <div className="h-[300px]">
                  {metrics ? (
                    <ResponsiveContainer width="100%" height="100%">
                      <AreaChart
                        data={getTimeSeriesData()}
                        margin={{ top: 10, right: 30, left: 0, bottom: 0 }}
                      >
                        <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                        <XAxis dataKey="name" />
                        <YAxis tickFormatter={(value) => `${value}%`} />
                        <Tooltip formatter={(value) => [`${value}%`]} />
                        <Legend />
                        <Area
                          type="monotone"
                          dataKey="cpu"
                          name="CPU Usage"
                          stackId="1"
                          stroke="#3b82f6"
                          fill="#3b82f6"
                          fillOpacity={0.6}
                        />
                        <Area
                          type="monotone"
                          dataKey="memory"
                          name="Memory Usage"
                          stackId="2"
                          stroke="#22c55e"
                          fill="#22c55e"
                          fillOpacity={0.6}
                        />
                        <Area
                          type="monotone"
                          dataKey="disk"
                          name="Disk Usage"
                          stackId="3"
                          stroke="#eab308"
                          fill="#eab308"
                          fillOpacity={0.6}
                        />
                      </AreaChart>
                    </ResponsiveContainer>
                  ) : (
                    <Skeleton className="h-full w-full" />
                  )}
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="pods" className="space-y-4 mt-4">
            <div className="grid gap-4 md:grid-cols-2">
              <Card>
                <CardHeader>
                  <CardTitle>Pod Status Distribution</CardTitle>
                  <CardDescription>
                    Current status of all pods in the cluster
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <div className="h-[300px]">
                    {metrics ? (
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart
                          data={getPodStatusData()}
                          margin={{ top: 20, right: 30, left: 20, bottom: 5 }}
                        >
                          <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                          <XAxis dataKey="name" />
                          <YAxis />
                          <Tooltip />
                          <Legend />
                          <Bar
                            dataKey="value"
                            name="Pods"
                            fill="#8884d8"
                            radius={[4, 4, 0, 0]}
                          >
                            {getPodStatusData().map((entry, index) => (
                              <Cell key={`cell-${index}`} fill={entry.color} />
                            ))}
                          </Bar>
                        </BarChart>
                      </ResponsiveContainer>
                    ) : (
                      <Skeleton className="h-full w-full" />
                    )}
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>Node Status</CardTitle>
                  <CardDescription>
                    Current status of all nodes in the cluster
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <div className="grid grid-cols-2 gap-4">
                    <div className="flex flex-col items-center justify-center p-6 bg-muted rounded-lg">
                      <h3 className="text-xl font-semibold mb-2">Ready</h3>
                      <div className="text-4xl font-bold text-green-500">
                        {metrics ? metrics.nodeCount.ready : '-'}
                      </div>
                    </div>
                    <div className="flex flex-col items-center justify-center p-6 bg-muted rounded-lg">
                      <h3 className="text-xl font-semibold mb-2">Not Ready</h3>
                      <div className="text-4xl font-bold text-red-500">
                        {metrics ? metrics.nodeCount.notReady : '-'}
                      </div>
                    </div>
                  </div>

                  <div className="mt-6">
                    <h3 className="text-sm font-medium mb-2">Node Readiness</h3>
                    <div className="w-full bg-muted rounded-full h-2.5">
                      {metrics && (
                        <div
                          className="bg-green-500 h-2.5 rounded-full"
                          style={{
                            width: `${(metrics.nodeCount.ready / metrics.nodeCount.total) * 100}%`
                          }}
                        ></div>
                      )}
                    </div>
                    <div className="flex justify-between text-xs text-muted-foreground mt-1">
                      <span>0%</span>
                      <span>
                        {metrics
                          ? `${Math.round((metrics.nodeCount.ready / metrics.nodeCount.total) * 100)}%`
                          : '0%'}
                      </span>
                      <span>100%</span>
                    </div>
                  </div>
                </CardContent>
              </Card>
            </div>

            <Card>
              <CardHeader>
                <CardTitle>Pod Metrics</CardTitle>
                <CardDescription>
                  Detailed metrics about pod statuses and health
                </CardDescription>
              </CardHeader>
              <CardContent>
                <div className="grid gap-4 md:grid-cols-4">
                  <div className="space-y-1">
                    <p className="text-sm font-medium">Total Pods</p>
                    <p className="text-2xl font-bold">
                      {metrics ? metrics.pods.total : '-'}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm font-medium">Running</p>
                    <p className="text-2xl font-bold text-green-500">
                      {metrics ? metrics.pods.running : '-'}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm font-medium">Pending</p>
                    <p className="text-2xl font-bold text-amber-500">
                      {metrics ? metrics.pods.pending : '-'}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm font-medium">Failed / CrashLoop</p>
                    <p className="text-2xl font-bold text-red-500">
                      {metrics ? (metrics.pods.failed + metrics.pods.crashLoopBackOff) : '-'}
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="network" className="space-y-4 mt-4">
            <div className="grid gap-4 md:grid-cols-2">
              <Card>
                <CardHeader>
                  <CardTitle>Network Traffic</CardTitle>
                  <CardDescription>
                    Network traffic over time
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <div className="h-[300px]">
                    {metrics ? (
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart
                          data={getNetworkTimeSeriesData()}
                          margin={{ top: 20, right: 30, left: 20, bottom: 5 }}
                        >
                          <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                          <XAxis dataKey="name" />
                          <YAxis />
                          <Tooltip />
                          <Legend />
                          <Line
                            type="monotone"
                            dataKey="traffic"
                            name="Traffic (MB/s)"
                            stroke="#8b5cf6"
                            activeDot={{ r: 8 }}
                          />
                        </LineChart>
                      </ResponsiveContainer>
                    ) : (
                      <Skeleton className="h-full w-full" />
                    )}
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>Network I/O</CardTitle>
                  <CardDescription>
                    Network receive and transmit rates
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <div className="h-[300px]">
                    {metrics ? (
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart
                          data={[
                            { name: 'Receive', value: metrics.networkReceive[metrics.networkReceive.length - 1] },
                            { name: 'Transmit', value: metrics.networkTransmit[metrics.networkTransmit.length - 1] }
                          ]}
                          margin={{ top: 20, right: 30, left: 20, bottom: 5 }}
                        >
                          <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                          <XAxis dataKey="name" />
                          <YAxis />
                          <Tooltip />
                          <Legend />
                          <Bar
                            dataKey="value"
                            name="MB/s"
                            fill="#8b5cf6"
                            radius={[4, 4, 0, 0]}
                          />
                        </BarChart>
                      </ResponsiveContainer>
                    ) : (
                      <Skeleton className="h-full w-full" />
                    )}
                  </div>
                </CardContent>
              </Card>
            </div>

            <Card>
              <CardHeader>
                <CardTitle>Traffic Metrics</CardTitle>
                <CardDescription>
                  Detailed metrics about network traffic
                </CardDescription>
              </CardHeader>
              <CardContent>
                <div className="grid gap-4 md:grid-cols-3">
                  <div className="space-y-1">
                    <p className="text-sm font-medium">Total Traffic</p>
                    <p className="text-2xl font-bold flex items-center">
                      <Activity className="h-5 w-5 mr-1 text-primary" />
                      {metrics ? `${metrics.networkTotal} MB/s` : '-'}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm font-medium">Receive Rate</p>
                    <p className="text-2xl font-bold">
                      {metrics ? `${metrics.networkReceive[metrics.networkReceive.length - 1]} MB/s` : '-'}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm font-medium">Transmit Rate</p>
                    <p className="text-2xl font-bold">
                      {metrics ? `${metrics.networkTransmit[metrics.networkTransmit.length - 1]} MB/s` : '-'}
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
      </div>
    </DashboardLayout>
  );
}

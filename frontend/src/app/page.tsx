"use client";

import { useEffect, useState } from "react";
import { DashboardLayout } from "@/components/layout/dashboard-layout";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import {
  AlertTriangle,
  CheckCircle2,
  XCircle,
  Server,
  Package2,
  Cpu,
  Memory
} from "lucide-react";
import apiService, { ClusterHealth } from "@/lib/api";
import {
  formatNumber,
  formatPercent,
  getHealthBadgeVariant,
  getHealthColor
} from "@/lib/utils";

export default function DashboardPage() {
  const [clusterStatus, setClusterStatus] = useState<ClusterHealth | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchClusterHealth = async () => {
      try {
        setLoading(true);
        const data = await apiService.getClusterHealth();
        setClusterStatus(data);
      } catch (error) {
        console.error("Failed to fetch cluster health:", error);
      } finally {
        setLoading(false);
      }
    };

    fetchClusterHealth();

    // Set up automatic refreshing
    const intervalId = setInterval(fetchClusterHealth, 30000); // refresh every 30 seconds

    return () => clearInterval(intervalId);
  }, []);

  // Function to get the appropriate health icon
  const getHealthIcon = (status: string) => {
    switch (status.toLowerCase()) {
      case "healthy":
        return <CheckCircle2 className="h-5 w-5 text-green-500" />;
      case "degraded":
        return <AlertTriangle className="h-5 w-5 text-orange-500" />;
      case "critical":
        return <XCircle className="h-5 w-5 text-red-500" />;
      default:
        return null;
    }
  };

  if (loading || !clusterStatus) {
    return (
      <DashboardLayout>
        <div className="flex items-center justify-center h-[calc(100vh-100px)]">
          <div className="text-center">
            <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary mx-auto"></div>
            <p className="mt-4 text-lg">Loading cluster status...</p>
          </div>
        </div>
      </DashboardLayout>
    );
  }

  return (
    <DashboardLayout>
      <div className="space-y-6">
        <div className="flex flex-col md:flex-row md:items-center md:justify-between">
          <h1 className="text-2xl font-bold">Cluster Overview</h1>
          <div className="flex flex-wrap items-center gap-2 mt-2 md:mt-0">
            <Badge
              variant={getHealthBadgeVariant(clusterStatus.status)}
              className="flex items-center gap-1"
            >
              {getHealthIcon(clusterStatus.status)}
              Cluster: {clusterStatus.status.charAt(0).toUpperCase() + clusterStatus.status.slice(1)}
            </Badge>

            <Badge
              variant={clusterStatus.prometheus ? "outline" : "destructive"}
              className="flex items-center gap-1"
            >
              {clusterStatus.prometheus ?
                <CheckCircle2 className="h-3.5 w-3.5" /> :
                <XCircle className="h-3.5 w-3.5" />
              }
              Prometheus: {clusterStatus.prometheus ? "Connected" : "Disconnected"}
            </Badge>

            <Badge
              variant={clusterStatus.agentAutoRemediation ? "outline" : "secondary"}
              className="flex items-center gap-1"
            >
              Auto-Remediation: {clusterStatus.agentAutoRemediation ? "Enabled" : "Disabled"}
            </Badge>
          </div>
        </div>

        {clusterStatus.status !== "healthy" && (
          <Alert variant={clusterStatus.status === "critical" ? "destructive" : "warning"}>
            <AlertTriangle className="h-4 w-4" />
            <AlertTitle>
              {clusterStatus.status === "critical" ? "Critical Issue Detected" : "Warning"}
            </AlertTitle>
            <AlertDescription>
              {clusterStatus.clusterHealth}
            </AlertDescription>
          </Alert>
        )}

        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
          <Card>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium">Nodes</CardTitle>
              <Server className="h-4 w-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">
                {clusterStatus.nodes.ready} / {clusterStatus.nodes.total}
              </div>
              <p className="text-xs text-muted-foreground">
                {formatPercent(clusterStatus.nodes.ready / clusterStatus.nodes.total * 100)} nodes ready
              </p>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium">Pods</CardTitle>
              <Package2 className="h-4 w-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">
                {clusterStatus.pods.running} / {clusterStatus.pods.total}
              </div>
              <p className="text-xs text-muted-foreground">
                {formatPercent(clusterStatus.pods.running / clusterStatus.pods.total * 100)} pods running
              </p>
              <div className="mt-2 text-xs flex flex-col gap-1">
                <div className="flex justify-between">
                  <span>Pending:</span>
                  <span className="font-medium">{clusterStatus.pods.pending}</span>
                </div>
                <div className="flex justify-between">
                  <span>Failed:</span>
                  <span className="font-medium text-red-500">{clusterStatus.pods.failed}</span>
                </div>
                <div className="flex justify-between">
                  <span>CrashLoopBackOff:</span>
                  <span className="font-medium text-red-500">{clusterStatus.pods.crashLoopBackOff}</span>
                </div>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium">CPU Usage</CardTitle>
              <Cpu className="h-4 w-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">
                {formatPercent(clusterStatus.cpuUsagePercent)}
              </div>
              <Progress
                value={clusterStatus.cpuUsagePercent}
                className="h-2 mt-2"
                indicatorClassName={
                  clusterStatus.cpuUsagePercent > 90 ? "bg-red-500" :
                  clusterStatus.cpuUsagePercent > 75 ? "bg-orange-500" :
                  "bg-green-500"
                }
              />
              <p className="text-xs text-muted-foreground mt-2">
                {clusterStatus.cpuUsagePercent > 90 ? "High CPU pressure" :
                 clusterStatus.cpuUsagePercent > 75 ? "Moderate CPU usage" :
                 "Healthy CPU usage"}
              </p>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium">Memory Usage</CardTitle>
              <Memory className="h-4 w-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">
                {formatPercent(clusterStatus.memoryUsagePercent)}
              </div>
              <Progress
                value={clusterStatus.memoryUsagePercent}
                className="h-2 mt-2"
                indicatorClassName={
                  clusterStatus.memoryUsagePercent > 90 ? "bg-red-500" :
                  clusterStatus.memoryUsagePercent > 75 ? "bg-orange-500" :
                  "bg-green-500"
                }
              />
              <p className="text-xs text-muted-foreground mt-2">
                {clusterStatus.memoryUsagePercent > 90 ? "High memory pressure" :
                 clusterStatus.memoryUsagePercent > 75 ? "Moderate memory usage" :
                 "Healthy memory usage"}
              </p>
            </CardContent>
          </Card>
        </div>

        <Card>
          <CardHeader>
            <CardTitle>Recent Alerts</CardTitle>
            <CardDescription>Latest alerts from the monitoring system</CardDescription>
          </CardHeader>
          <CardContent>
            {clusterStatus.status === "healthy" ? (
              <div className="flex items-center justify-center h-40">
                <div className="text-center">
                  <CheckCircle2 className="h-8 w-8 text-green-500 mx-auto" />
                  <p className="mt-2 text-muted-foreground">
                    No alerts detected. Cluster is running smoothly.
                  </p>
                </div>
              </div>
            ) : (
              <div className="space-y-4">
                <Alert variant={clusterStatus.status === "critical" ? "destructive" : "warning"}>
                  <AlertTriangle className="h-4 w-4" />
                  <AlertTitle>
                    {clusterStatus.status === "critical" ? "Critical" : "Warning"}
                  </AlertTitle>
                  <AlertDescription>
                    {clusterStatus.clusterHealth}
                  </AlertDescription>
                </Alert>

                {(clusterStatus.pods.failed > 0 || clusterStatus.pods.crashLoopBackOff > 0) && (
                  <Alert variant="destructive">
                    <XCircle className="h-4 w-4" />
                    <AlertTitle>Pod Failures</AlertTitle>
                    <AlertDescription>
                      {clusterStatus.pods.failed > 0 && `${clusterStatus.pods.failed} failed pods. `}
                      {clusterStatus.pods.crashLoopBackOff > 0 && `${clusterStatus.pods.crashLoopBackOff} pods in CrashLoopBackOff state. `}
                      Check the Pods section for details.
                    </AlertDescription>
                  </Alert>
                )}

                {(clusterStatus.nodes.total - clusterStatus.nodes.ready) > 0 && (
                  <Alert variant="destructive">
                    <AlertTriangle className="h-4 w-4" />
                    <AlertTitle>Node Issues</AlertTitle>
                    <AlertDescription>
                      {clusterStatus.nodes.total - clusterStatus.nodes.ready} nodes are not in Ready state.
                    </AlertDescription>
                  </Alert>
                )}
              </div>
            )}
          </CardContent>
          <CardFooter>
            <p className="text-xs text-muted-foreground">
              Last updated: {new Date().toLocaleTimeString()}
            </p>
          </CardFooter>
        </Card>
      </div>
    </DashboardLayout>
  );
}

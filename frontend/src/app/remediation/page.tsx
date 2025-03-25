"use client";

import { useState } from "react";
import { DashboardLayout } from "@/components/layout/dashboard-layout";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AlertCircle, CheckCircle2, RefreshCw, Scale, XCircle } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

// Sample data - in a real app, this would be fetched from the API
const remediationHistory = [
  {
    time: "2023-05-12T15:30:00Z",
    action: "Restarted pod inventory-5f9d4bcdf7-kl8pz",
    reason: "CrashLoopBackOff detected",
    initiatedBy: "auto",
    status: "success"
  },
  {
    time: "2023-05-12T14:45:10Z",
    action: "Scaled deployment checkout to 5 replicas",
    reason: "High CPU usage (85%)",
    initiatedBy: "auto",
    status: "success"
  },
  {
    time: "2023-05-12T13:20:00Z",
    action: "Scaled deployment checkout to 4 replicas",
    reason: "High CPU usage (80%)",
    initiatedBy: "auto",
    status: "success"
  },
  {
    time: "2023-05-12T12:10:30Z",
    action: "Update resources for payment-service",
    reason: "Memory pressure (90%)",
    initiatedBy: "manual",
    status: "success"
  },
  {
    time: "2023-05-12T10:05:15Z",
    action: "Cordon node worker-3",
    reason: "Disk pressure",
    initiatedBy: "auto",
    status: "failed",
    error: "Permission denied"
  }
];

// Sample common actions for the manual remediation section
const commonActions = [
  {
    label: "Restart Failed Pods",
    action: "restartPod",
    target: "all-failed",
    description: "Restart all pods in CrashLoopBackOff or Failed state",
    icon: RefreshCw
  },
  {
    label: "Scale Frontend",
    action: "scaleDeployment",
    target: "frontend",
    parameters: { replicas: 3 },
    description: "Scale the frontend deployment to 3 replicas",
    icon: Scale
  }
];

export default function RemediationPage() {
  const [activeTab, setActiveTab] = useState("history");

  // Format date for display
  const formatDate = (dateString: string) => {
    const date = new Date(dateString);
    return new Intl.DateTimeFormat('en-US', {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: 'numeric',
      hour12: true
    }).format(date);
  };

  // Get badge variant based on status
  const getStatusBadge = (status: string, initiatedBy: string) => {
    if (status === "success") {
      return (
        <Badge variant="success" className="flex items-center gap-1">
          <CheckCircle2 className="h-3 w-3" />
          Success
        </Badge>
      );
    }
    if (status === "failed") {
      return (
        <Badge variant="destructive" className="flex items-center gap-1">
          <XCircle className="h-3 w-3" />
          Failed
        </Badge>
      );
    }
    return (
      <Badge variant={initiatedBy === "auto" ? "default" : "secondary"} className="flex items-center gap-1">
        {initiatedBy.charAt(0).toUpperCase() + initiatedBy.slice(1)}
      </Badge>
    );
  };

  // Handle click on manual remediation actions
  const handleActionClick = (action: typeof commonActions[0]) => {
    // In a real app, this would trigger an API call to perform the action
    console.log(`Performing action: ${action.label}`, action);
    // Show a toast notification or some feedback
  };

  return (
    <DashboardLayout>
      <div className="space-y-6">
        <div className="flex justify-between items-center">
          <h1 className="text-2xl font-bold">Remediation</h1>
        </div>

        <Tabs defaultValue="history" className="w-full" onValueChange={setActiveTab}>
          <TabsList>
            <TabsTrigger value="history">Remediation History</TabsTrigger>
            <TabsTrigger value="manual">Manual Actions</TabsTrigger>
          </TabsList>

          <TabsContent value="history" className="space-y-4">
            <Card>
              <CardHeader>
                <CardTitle>Remediation Events</CardTitle>
                <CardDescription>
                  Recent automatic and manual remediation actions
                </CardDescription>
              </CardHeader>
              <CardContent>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Time</TableHead>
                      <TableHead>Action</TableHead>
                      <TableHead>Reason</TableHead>
                      <TableHead>Initiated By</TableHead>
                      <TableHead>Status</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {remediationHistory.map((event, index) => (
                      <TableRow key={index}>
                        <TableCell>{formatDate(event.time)}</TableCell>
                        <TableCell>{event.action}</TableCell>
                        <TableCell>{event.reason}</TableCell>
                        <TableCell>
                          <Badge variant={event.initiatedBy === "auto" ? "outline" : "secondary"}>
                            {event.initiatedBy}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          {getStatusBadge(event.status, event.initiatedBy)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="manual" className="space-y-4">
            <Card>
              <CardHeader>
                <CardTitle>Manual Remediation Actions</CardTitle>
                <CardDescription>
                  Trigger remediation actions manually
                </CardDescription>
              </CardHeader>
              <CardContent>
                <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
                  {commonActions.map((action, index) => (
                    <Card key={index} className="bg-muted/40">
                      <CardHeader className="pb-2">
                        <div className="flex items-center justify-between">
                          <CardTitle className="text-sm font-medium flex items-center gap-2">
                            <action.icon className="h-4 w-4 text-primary" />
                            {action.label}
                          </CardTitle>
                        </div>
                      </CardHeader>
                      <CardContent>
                        <p className="text-xs text-muted-foreground mb-4">{action.description}</p>
                        <Button
                          size="sm"
                          className="w-full"
                          onClick={() => handleActionClick(action)}
                        >
                          Execute
                        </Button>
                      </CardContent>
                    </Card>
                  ))}

                  {/* Custom action card */}
                  <Card className="bg-muted/40">
                    <CardHeader className="pb-2">
                      <div className="flex items-center justify-between">
                        <CardTitle className="text-sm font-medium">Custom Action</CardTitle>
                      </div>
                    </CardHeader>
                    <CardContent>
                      <p className="text-xs text-muted-foreground mb-4">
                        Create a custom remediation action (advanced)
                      </p>
                      <Button
                        variant="outline"
                        size="sm"
                        className="w-full"
                      >
                        Create Custom Action
                      </Button>
                    </CardContent>
                  </Card>
                </div>
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
      </div>
    </DashboardLayout>
  );
}

"use client";

import { useState } from "react";
import { DashboardLayout } from "@/components/layout/dashboard-layout";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import {
  Settings,
  AlertCircle,
  InfoIcon,
  Save,
  Bot,
  RefreshCw
} from "lucide-react";

export default function SettingsPage() {
  // State for general settings
  const [autoRemediation, setAutoRemediation] = useState(true);
  const [alertsEnabled, setAlertsEnabled] = useState(true);
  const [prometheusEndpoint, setPrometheusEndpoint] = useState("http://prometheus.monitoring:9090");
  const [refreshInterval, setRefreshInterval] = useState("30");
  const [apiEndpoint, setApiEndpoint] = useState("http://localhost:5000");

  // State for AI settings
  const [aiEnabled, setAiEnabled] = useState(true);
  const [selectedModel, setSelectedModel] = useState("gemini-pro");
  const [confidence, setConfidence] = useState("0.7");
  const [maxRemediation, setMaxRemediation] = useState("5");

  // State for save button
  const [saveStatus, setSaveStatus] = useState<null | "success" | "error">(null);

  // Handle form submission
  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    // In a real app, this would send the settings to the API
    console.log({
      autoRemediation,
      alertsEnabled,
      prometheusEndpoint,
      refreshInterval: parseInt(refreshInterval),
      apiEndpoint,
      aiEnabled,
      selectedModel,
      confidence: parseFloat(confidence),
      maxRemediation: parseInt(maxRemediation)
    });

    // Simulate success
    setSaveStatus("success");
    setTimeout(() => setSaveStatus(null), 3000);
  };

  // Handle test connection to Prometheus
  const testPrometheusConnection = () => {
    // In a real app, this would test the connection to Prometheus
    console.log(`Testing connection to ${prometheusEndpoint}`);
    // Show an alert or toast with the result
  };

  return (
    <DashboardLayout>
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold">Settings</h1>
        </div>

        <form onSubmit={handleSubmit}>
          <Tabs defaultValue="general" className="w-full">
            <TabsList className="grid w-full grid-cols-2">
              <TabsTrigger value="general" className="flex items-center gap-2">
                <Settings className="h-4 w-4" />
                General
              </TabsTrigger>
              <TabsTrigger value="ai" className="flex items-center gap-2">
                <Bot className="h-4 w-4" />
                AI Configuration
              </TabsTrigger>
            </TabsList>

            <TabsContent value="general" className="space-y-4 mt-4">
              <Card>
                <CardHeader>
                  <CardTitle>General Settings</CardTitle>
                  <CardDescription>
                    Configure general application settings
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="flex items-center justify-between">
                    <div className="space-y-0.5">
                      <Label htmlFor="auto-remediation">Auto Remediation</Label>
                      <p className="text-sm text-muted-foreground">
                        Allow the system to automatically apply remediation actions
                      </p>
                    </div>
                    <Switch
                      id="auto-remediation"
                      checked={autoRemediation}
                      onCheckedChange={setAutoRemediation}
                    />
                  </div>

                  <Separator />

                  <div className="flex items-center justify-between">
                    <div className="space-y-0.5">
                      <Label htmlFor="alerts-enabled">Alert Notifications</Label>
                      <p className="text-sm text-muted-foreground">
                        Enable alert notifications for critical issues
                      </p>
                    </div>
                    <Switch
                      id="alerts-enabled"
                      checked={alertsEnabled}
                      onCheckedChange={setAlertsEnabled}
                    />
                  </div>

                  <Separator />

                  <div className="grid gap-2">
                    <Label htmlFor="prometheus-endpoint">Prometheus Endpoint</Label>
                    <div className="flex gap-2">
                      <Input
                        id="prometheus-endpoint"
                        value={prometheusEndpoint}
                        onChange={(e) => setPrometheusEndpoint(e.target.value)}
                        placeholder="http://prometheus:9090"
                      />
                      <Button
                        type="button"
                        variant="outline"
                        onClick={testPrometheusConnection}
                      >
                        Test
                      </Button>
                    </div>
                  </div>

                  <div className="grid gap-2">
                    <Label htmlFor="refresh-interval">Metrics Refresh Interval (seconds)</Label>
                    <Input
                      id="refresh-interval"
                      type="number"
                      value={refreshInterval}
                      onChange={(e) => setRefreshInterval(e.target.value)}
                      min="5"
                      max="300"
                    />
                  </div>

                  <div className="grid gap-2">
                    <Label htmlFor="api-endpoint">API Endpoint</Label>
                    <Input
                      id="api-endpoint"
                      value={apiEndpoint}
                      onChange={(e) => setApiEndpoint(e.target.value)}
                      placeholder="http://localhost:5000"
                    />
                  </div>
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="ai" className="space-y-4 mt-4">
              <Card>
                <CardHeader>
                  <CardTitle>AI Agent Configuration</CardTitle>
                  <CardDescription>
                    Configure the AI agent for automated decision making
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="flex items-center justify-between">
                    <div className="space-y-0.5">
                      <Label htmlFor="ai-enabled">AI Agent Enabled</Label>
                      <p className="text-sm text-muted-foreground">
                        Use AI to analyze and suggest remediation actions
                      </p>
                    </div>
                    <Switch
                      id="ai-enabled"
                      checked={aiEnabled}
                      onCheckedChange={setAiEnabled}
                    />
                  </div>

                  <Separator />

                  <div className="grid gap-2">
                    <Label htmlFor="llm-model">LLM Model</Label>
                    <Select value={selectedModel} onValueChange={setSelectedModel}>
                      <SelectTrigger id="llm-model">
                        <SelectValue placeholder="Select model" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="gemini-pro">Gemini Pro</SelectItem>
                        <SelectItem value="gpt-4">GPT-4</SelectItem>
                        <SelectItem value="claude-3">Claude 3</SelectItem>
                        <SelectItem value="local-model">Local Model</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>

                  <div className="grid gap-2">
                    <Label htmlFor="confidence-threshold">
                      Confidence Threshold (0.0 - 1.0)
                    </Label>
                    <Input
                      id="confidence-threshold"
                      type="number"
                      value={confidence}
                      onChange={(e) => setConfidence(e.target.value)}
                      min="0"
                      max="1"
                      step="0.05"
                    />
                    <p className="text-xs text-muted-foreground">
                      Minimum confidence level required for AI to suggest actions
                    </p>
                  </div>

                  <div className="grid gap-2">
                    <Label htmlFor="max-remediation">
                      Maximum Auto-Remediation Actions Per Hour
                    </Label>
                    <Input
                      id="max-remediation"
                      type="number"
                      value={maxRemediation}
                      onChange={(e) => setMaxRemediation(e.target.value)}
                      min="0"
                      max="50"
                    />
                    <p className="text-xs text-muted-foreground">
                      Limits the number of automatic remediation actions
                    </p>
                  </div>

                  <Alert className="mt-4">
                    <AlertCircle className="h-4 w-4" />
                    <AlertTitle>Important Note</AlertTitle>
                    <AlertDescription>
                      When AI agent is enabled with auto-remediation, the system will automatically
                      apply suggested fixes if confidence is above the threshold.
                    </AlertDescription>
                  </Alert>
                </CardContent>
              </Card>
            </TabsContent>
          </Tabs>

          <div className="mt-6 flex justify-between items-center">
            {saveStatus === "success" && (
              <Alert variant="success">
                <InfoIcon className="h-4 w-4" />
                <AlertTitle>Settings Saved</AlertTitle>
                <AlertDescription>
                  Your settings have been successfully saved.
                </AlertDescription>
              </Alert>
            )}

            {saveStatus === "error" && (
              <Alert variant="destructive">
                <AlertCircle className="h-4 w-4" />
                <AlertTitle>Error</AlertTitle>
                <AlertDescription>
                  There was an error saving your settings. Please try again.
                </AlertDescription>
              </Alert>
            )}

            {saveStatus === null && <div></div>}

            <Button type="submit" className="flex items-center gap-2">
              <Save className="h-4 w-4" />
              Save Settings
            </Button>
          </div>
        </form>
      </div>
    </DashboardLayout>
  );
}

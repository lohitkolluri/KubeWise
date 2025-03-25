"use client";

import { ReactNode, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { ThemeToggle } from "@/components/theme-toggle";
import {
  HomeIcon,
  BarChart3Icon,
  WrenchIcon,
  SettingsIcon,
  MenuIcon,
  XIcon,
  AlertTriangleIcon,
  GithubIcon
} from "lucide-react";

interface DashboardLayoutProps {
  children: ReactNode;
}

export function DashboardLayout({ children }: DashboardLayoutProps) {
  const pathname = usePathname();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  const navigation = [
    { name: "Home", href: "/", icon: HomeIcon },
    { name: "Metrics", href: "/metrics", icon: BarChart3Icon },
    { name: "Remediation", href: "/remediation", icon: WrenchIcon },
    { name: "Settings", href: "/settings", icon: SettingsIcon },
  ];

  return (
    <div className="flex h-full min-h-screen bg-background">
      {/* Sidebar for desktop */}
      <div className="hidden md:fixed md:inset-y-0 md:z-50 md:flex md:w-72 md:flex-col">
        <div className="flex grow flex-col gap-y-5 overflow-y-auto border-r bg-background px-6 py-4">
          <div className="flex h-16 shrink-0 items-center justify-between">
            <Link href="/" className="flex items-center">
              <AlertTriangleIcon className="h-8 w-8 text-primary" />
              <span className="ml-2 text-xl font-bold">K8s Monitor</span>
            </Link>
            <ThemeToggle />
          </div>
          <nav className="flex flex-1 flex-col">
            <ul className="flex flex-1 flex-col gap-y-7">
              <li>
                <ul className="space-y-1">
                  {navigation.map((item) => (
                    <li key={item.name}>
                      <Link
                        href={item.href}
                        className={cn(
                          pathname === item.href
                            ? "bg-accent text-accent-foreground"
                            : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                          "group flex gap-x-3 rounded-md p-3 text-sm font-semibold leading-6"
                        )}
                      >
                        <item.icon className="h-5 w-5 shrink-0" aria-hidden="true" />
                        {item.name}
                      </Link>
                    </li>
                  ))}
                </ul>
              </li>
              <li className="mt-auto">
                <a
                  href="https://github.com/yourusername/k8s-prediction-model"
                  target="_blank"
                  rel="noreferrer"
                  className="group flex gap-x-3 rounded-md p-3 text-sm font-semibold leading-6 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                >
                  <GithubIcon className="h-5 w-5 shrink-0" aria-hidden="true" />
                  GitHub
                </a>
              </li>
            </ul>
          </nav>
        </div>
      </div>

      {/* Mobile menu */}
      <div className="md:hidden">
        <div className="fixed inset-0 z-40 flex">
          <div
            className={cn(
              "fixed inset-0 bg-background/80 backdrop-blur-sm",
              mobileMenuOpen ? "block" : "hidden"
            )}
            onClick={() => setMobileMenuOpen(false)}
          />
          <div
            className={cn(
              "fixed inset-y-0 left-0 z-40 w-72 overflow-y-auto bg-background p-6 sm:max-w-xs transition-transform duration-300 ease-in-out",
              mobileMenuOpen ? "translate-x-0" : "-translate-x-full"
            )}
          >
            <div className="flex items-center justify-between">
              <Link href="/" className="flex items-center" onClick={() => setMobileMenuOpen(false)}>
                <AlertTriangleIcon className="h-8 w-8 text-primary" />
                <span className="ml-2 text-xl font-bold">K8s Monitor</span>
              </Link>
              <Button
                variant="ghost"
                size="icon"
                onClick={() => setMobileMenuOpen(false)}
              >
                <XIcon className="h-6 w-6" />
              </Button>
            </div>
            <nav className="mt-6">
              <ul className="space-y-1">
                {navigation.map((item) => (
                  <li key={item.name}>
                    <Link
                      href={item.href}
                      className={cn(
                        pathname === item.href
                          ? "bg-accent text-accent-foreground"
                          : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                        "group flex gap-x-3 rounded-md p-3 text-sm font-semibold leading-6"
                      )}
                      onClick={() => setMobileMenuOpen(false)}
                    >
                      <item.icon className="h-5 w-5 shrink-0" aria-hidden="true" />
                      {item.name}
                    </Link>
                  </li>
                ))}
              </ul>
              <div className="mt-10 flex items-center justify-between">
                <a
                  href="https://github.com/yourusername/k8s-prediction-model"
                  target="_blank"
                  rel="noreferrer"
                  className="group flex gap-x-3 rounded-md p-3 text-sm font-semibold leading-6 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                >
                  <GithubIcon className="h-5 w-5 shrink-0" aria-hidden="true" />
                  GitHub
                </a>
                <ThemeToggle />
              </div>
            </nav>
          </div>
        </div>
      </div>

      {/* Mobile header */}
      <div className="sticky top-0 z-40 flex h-16 shrink-0 items-center gap-x-4 border-b bg-background px-4 md:hidden">
        <Button
          variant="ghost"
          size="icon"
          onClick={() => setMobileMenuOpen(true)}
        >
          <MenuIcon className="h-6 w-6" />
        </Button>
        <div className="flex flex-1 items-center justify-between">
          <Link href="/" className="flex items-center">
            <AlertTriangleIcon className="h-6 w-6 text-primary" />
            <span className="ml-2 text-xl font-bold">K8s Monitor</span>
          </Link>
          <ThemeToggle />
        </div>
      </div>

      {/* Main content */}
      <div className="flex flex-1 flex-col md:pl-72">
        <main className="px-4 py-6 md:px-8 md:py-10">
          {children}
        </main>
      </div>
    </div>
  );
}

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

// Icons
import {
  BarChart3,
  LayoutDashboard,
  Settings,
  AlertCircle,
} from "lucide-react";

const navigation = [
  { name: "Dashboard", href: "/", icon: LayoutDashboard },
  { name: "Metrics", href: "/metrics", icon: BarChart3 },
  { name: "Remediation", href: "/remediation", icon: AlertCircle },
  { name: "Settings", href: "/settings", icon: Settings },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <div className="flex h-full flex-col bg-background border-r">
      <div className="flex flex-col px-3 py-4">
        <div className="mb-6 flex items-center px-2">
          <h2 className="text-xl font-semibold">K8s Monitor</h2>
        </div>
        <nav className="space-y-1 flex-1">
          {navigation.map((item) => {
            const isActive = pathname === item.href;
            return (
              <Link
                key={item.name}
                href={item.href}
                className={cn(
                  "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium",
                  isActive
                    ? "bg-secondary text-primary"
                    : "text-muted-foreground hover:bg-accent hover:text-primary"
                )}
              >
                <item.icon className="h-5 w-5 shrink-0" aria-hidden="true" />
                {item.name}
              </Link>
            );
          })}
        </nav>
      </div>
      <div className="mt-auto border-t p-3">
        <div className="flex items-center justify-between">
          <div className="text-xs text-muted-foreground">
            v1.0
          </div>
        </div>
      </div>
    </div>
  );
}

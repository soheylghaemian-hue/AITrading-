import "./globals.css";
import type { Metadata } from "next";
import { DashboardProvider, AppShell } from "@/components/shell";

export const metadata: Metadata = {
  title: "GIGBAY AI — Command Center",
  description: "Institutional AI trading command center — read-only, no fabricated data.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <DashboardProvider>
          <AppShell>{children}</AppShell>
        </DashboardProvider>
      </body>
    </html>
  );
}

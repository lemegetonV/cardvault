import type { Metadata, Viewport } from "next";
import { Providers } from "@/components/providers";
import { operatorPublicUrl } from "@/lib/server/config";
import "./globals.css";

// Rendered per request so OPERATOR_PUBLIC_URL is read at run time, not baked into the build.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "CardVault Bank",
  description: "CardVault Bank: cards and the checkout wallet (a simulation)",
};

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers config={{ operatorUrl: operatorPublicUrl }}>{children}</Providers>
      </body>
    </html>
  );
}

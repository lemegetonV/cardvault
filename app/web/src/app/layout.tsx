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

// Seeded UI bugs (all off by default): the BUG_UI_* flags that are "true" at run time.
const uiBugs = () =>
  Object.keys(process.env).filter(
    (k) => k.startsWith("BUG_UI_") && process.env[k]?.toLowerCase() === "true",
  );

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers config={{ operatorUrl: operatorPublicUrl, bugs: uiBugs() }}>{children}</Providers>
      </body>
    </html>
  );
}

import type { Metadata } from "next";
import { Fira_Code, Mona_Sans, Oswald } from "next/font/google";
import { Nav } from "@/components/Nav";
import "./globals.css";

const oswald = Oswald({
  weight: ["300", "400"],
  subsets: ["latin"],
  variable: "--font-oswald",
});

const monaSans = Mona_Sans({
  subsets: ["latin"],
  variable: "--font-mona",
});

const firaCode = Fira_Code({
  subsets: ["latin"],
  variable: "--font-fira",
});

export const metadata: Metadata = {
  title: "HADES — Smart money intelligence",
  description: "Solana memecoin smart-money & insider intelligence",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`${oswald.variable} ${monaSans.variable} ${firaCode.variable}`}>
      <body className="min-h-dvh">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded focus:bg-primary focus:px-3 focus:py-2 focus:text-fg"
        >
          Skip to content
        </a>
        <Nav />
        <main id="main" className="mx-auto w-full max-w-7xl px-4 pb-24 sm:px-6 lg:px-8">
          {children}
        </main>
        <footer className="border-t border-border">
          <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-2 px-4 py-6 text-xs text-muted sm:px-6 lg:px-8">
            <span className="font-display uppercase tracking-[0.2em]">Hades</span>
            <span>Read-only intelligence · no keys, no signing</span>
          </div>
        </footer>
      </body>
    </html>
  );
}

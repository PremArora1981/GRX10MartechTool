import type { Metadata } from "next";
import { ReportsClient } from "./_components/ReportsClient";

/**
 * Reports page — server component shell.
 *
 * All interactivity (generate buttons, cart, download links) lives in the
 * client component below. The server shell exists solely to export metadata
 * (not possible inside "use client" modules) and to keep the RSC boundary clean.
 */
export const metadata: Metadata = { title: "Reports" };

export default function ReportsPage() {
  return <ReportsClient />;
}

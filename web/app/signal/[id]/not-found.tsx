import Link from "next/link";

export default function SignalNotFound() {
  return (
    <div className="flex flex-col gap-4 pt-16">
      <h1 className="font-display text-4xl font-light uppercase tracking-tight">Sinyal tidak ditemukan</h1>
      <Link href="/signals" className="cursor-pointer text-fg underline decoration-primary decoration-2 underline-offset-4">
        Kembali ke daftar sinyal
      </Link>
    </div>
  );
}

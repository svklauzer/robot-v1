import Nav from "./Nav";
import ModeBanner from "./ModeBanner";

export default function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <main className="min-h-screen bg-[radial-gradient(circle_at_top_left,_rgba(16,185,129,0.14),_transparent_34%),linear-gradient(135deg,_#020617_0%,_#03140f_48%,_#020617_100%)] px-4 py-6 text-emerald-50 sm:px-6 lg:px-8">
      <div className="mx-auto max-w-7xl space-y-6">
        <ModeBanner />
        <Nav />
        {children}
      </div>
    </main>
  );
}

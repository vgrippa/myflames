import { NavLink, Route, Routes } from "react-router-dom";
import { AnimatePresence } from "framer-motion";
import { useLocation } from "react-router-dom";
import Catalog from "./routes/Catalog";
import Compare from "./routes/Compare";
import Detail from "./routes/Detail";

export default function App() {
  const location = useLocation();
  return (
    <div className="min-h-screen flex flex-col">
      <Header />
      <main className="flex-1 mx-auto w-full max-w-7xl px-6 py-8">
        <AnimatePresence mode="wait">
          <Routes location={location} key={location.pathname}>
            <Route path="/" element={<Catalog />} />
            <Route path="/compare" element={<Compare />} />
            <Route path="/algo/:name" element={<Detail />} />
          </Routes>
        </AnimatePresence>
      </main>
      <Footer />
    </div>
  );
}

function Header() {
  const linkBase =
    "px-3 py-1.5 rounded-md text-sm font-medium transition-colors";
  return (
    <header className="sticky top-0 z-30 backdrop-blur-md bg-bg/70 border-b border-border/60">
      <div className="mx-auto max-w-7xl px-6 py-3 flex items-center gap-6">
        <NavLink to="/" className="flex items-center gap-2 group">
          <span className="w-8 h-8 rounded-lg bg-gradient-to-br from-accent to-join grid place-items-center text-white font-bold shadow-glow">
            tx
          </span>
          <div className="leading-tight">
            <div className="text-sm font-semibold tracking-tight">Teach Explorer</div>
            <div className="text-[11px] text-muted">myflames algorithms catalog</div>
          </div>
        </NavLink>
        <nav className="ml-auto flex items-center gap-2">
          <NavLink
            to="/"
            end
            className={({ isActive }) =>
              `${linkBase} ${
                isActive ? "bg-card text-ink" : "text-muted hover:text-ink"
              }`
            }
          >
            Catalog
          </NavLink>
          <NavLink
            to="/compare"
            className={({ isActive }) =>
              `${linkBase} ${
                isActive ? "bg-card text-ink" : "text-muted hover:text-ink"
              }`
            }
          >
            Compare
          </NavLink>
          <a
            href="https://github.com/vgrippa/myflames"
            target="_blank"
            rel="noreferrer"
            className={`${linkBase} text-muted hover:text-ink`}
          >
            GitHub ↗
          </a>
        </nav>
      </div>
    </header>
  );
}

function Footer() {
  return (
    <footer className="border-t border-border/60 mt-12">
      <div className="mx-auto max-w-7xl px-6 py-6 text-xs text-muted flex flex-wrap items-center gap-3 justify-between">
        <div>
          21 algorithms from <code className="text-ink">myflames/teach</code>.
          Inspired by Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan FlameGraphs.
        </div>
        <div>MySQL 8.4 · MariaDB 11.4 · offline-capable</div>
      </div>
    </footer>
  );
}

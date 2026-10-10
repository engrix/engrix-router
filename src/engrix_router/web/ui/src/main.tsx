import { createRoot } from "react-dom/client";
import App from "./App";

// Titik masuk SPA. Katalog label sudah disuntik host (dashboard.py / dev
// plugin vite) ke window.__ENGRIX__ sebelum modul ini jalan. Stylesheet brand
// (engrix.css + app.css) ditautkan index.html dan diladeni gateway dari
// /static -- tidak di-bundle supaya tema pack tetap satu sumber.
const root = document.getElementById("root");
if (root) createRoot(root).render(<App />);

import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { LightboxProvider } from "./Lightbox";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <LightboxProvider>
      <App />
    </LightboxProvider>
  </React.StrictMode>,
);

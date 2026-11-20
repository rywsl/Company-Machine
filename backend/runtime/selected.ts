import { KiriProvider } from "./kiri";
import { PodmanProvider, type ContainerProvider } from "./podman";

export function selectedContainerProvider(name = process.env.MAO_CONTAINER_RUNTIME?.toLowerCase() ?? "podman"): ContainerProvider {
  const egressEnv = Object.fromEntries(["ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"].flatMap((key) => {
    const value = process.env[key];
    return value ? [[key, value]] : [];
  }));
  const options = { egressNetwork: process.env.MAO_EGRESS_NETWORK, egressEnv };
  if (name === "kiri") return new KiriProvider(options);
  if (name === "podman") return new PodmanProvider(options);
  throw new Error(`Unknown container runtime: ${name}`);
}

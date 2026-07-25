import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LoginScreen } from "./LoginScreen";
import { ApiError } from "@/core/transport";

vi.mock("@/core/transport", async () => {
  const actual = await vi.importActual<typeof import("@/core/transport")>("@/core/transport");
  return {
    ...actual,
    api: {
      loginPassword: vi.fn(),
      loginRecovery: vi.fn(),
      webauthnLoginOptions: vi.fn(),
      webauthnLoginVerify: vi.fn(),
    },
  };
});

describe("LoginScreen", () => {
  afterEach(() => vi.clearAllMocks());

  it("submits a password and calls onLoggedIn on success", async () => {
    const { api } = await import("@/core/transport");
    vi.mocked(api.loginPassword).mockResolvedValue({ ok: true });
    const onLoggedIn = vi.fn();
    render(<LoginScreen onLoggedIn={onLoggedIn} />);

    await userEvent.type(screen.getByPlaceholderText("Password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Sign in with password" }));

    await waitFor(() => expect(onLoggedIn).toHaveBeenCalled());
    expect(api.loginPassword).toHaveBeenCalledWith("hunter2");
  });

  it("shows the server's error message on a failed login without calling onLoggedIn", async () => {
    const { api } = await import("@/core/transport");
    vi.mocked(api.loginPassword).mockRejectedValue(new ApiError(401, "Incorrect password."));
    const onLoggedIn = vi.fn();
    render(<LoginScreen onLoggedIn={onLoggedIn} />);

    await userEvent.type(screen.getByPlaceholderText("Password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in with password" }));

    expect(await screen.findByText("Incorrect password.")).toBeInTheDocument();
    expect(onLoggedIn).not.toHaveBeenCalled();
  });

  it("switches to the recovery-code form and back", async () => {
    render(<LoginScreen onLoggedIn={vi.fn()} />);
    await userEvent.click(screen.getByText("Use a recovery code instead"));
    expect(screen.getByPlaceholderText("Recovery code")).toBeInTheDocument();

    await userEvent.click(screen.getByText("Back to password"));
    expect(screen.getByPlaceholderText("Password")).toBeInTheDocument();
  });

  it("shows a friendly message when the account is locked out (429)", async () => {
    const { api } = await import("@/core/transport");
    vi.mocked(api.loginPassword).mockRejectedValue(new ApiError(429, "Too many attempts", 12));
    render(<LoginScreen onLoggedIn={vi.fn()} />);

    await userEvent.type(screen.getByPlaceholderText("Password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in with password" }));

    expect(await screen.findByText("Too many attempts — try again in 12s.")).toBeInTheDocument();
  });
});

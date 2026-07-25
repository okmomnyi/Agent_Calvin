import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApprovalsList } from "./ApprovalsList";
import { useAppStore } from "@/core/store";
import { ApiError } from "@/core/transport";

vi.mock("@/core/transport", async () => {
  const actual = await vi.importActual<typeof import("@/core/transport")>("@/core/transport");
  return {
    ...actual,
    api: {
      resolveApproval: vi.fn(),
      webauthnLoginOptions: vi.fn(),
    },
  };
});
vi.mock("@/core/webauthnBrowser", () => ({
  authenticateWithPasskey: vi.fn(),
}));

describe("ApprovalsList", () => {
  beforeEach(() => {
    useAppStore.setState({
      pendingApprovals: [{ id: 7, title: "Apply: Backend Engineer", company: "Acme" }],
    });
  });
  afterEach(() => vi.clearAllMocks());

  it("renders nothing when there are no pending approvals", () => {
    useAppStore.setState({ pendingApprovals: [] });
    const { container } = render(<ApprovalsList />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders a pending approval's title and company", () => {
    render(<ApprovalsList />);
    expect(screen.getByText("Apply: Backend Engineer")).toBeInTheDocument();
    expect(screen.getByText("Acme")).toBeInTheDocument();
  });

  it("approving calls resolveApproval(true) and removes the row on success", async () => {
    const { api } = await import("@/core/transport");
    vi.mocked(api.resolveApproval).mockResolvedValue({ ok: true, status: "approved" });
    render(<ApprovalsList />);

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(useAppStore.getState().pendingApprovals).toHaveLength(0));
    expect(api.resolveApproval).toHaveBeenCalledWith(7, true);
  });

  it("denying calls resolveApproval(false)", async () => {
    const { api } = await import("@/core/transport");
    vi.mocked(api.resolveApproval).mockResolvedValue({ ok: true, status: "denied" });
    render(<ApprovalsList />);

    await userEvent.click(screen.getByRole("button", { name: "Deny" }));

    await waitFor(() => expect(api.resolveApproval).toHaveBeenCalledWith(7, false));
  });

  it("retries once with a fresh passkey assertion on step_up_required, then removes the row", async () => {
    const { api } = await import("@/core/transport");
    const { authenticateWithPasskey } = await import("@/core/webauthnBrowser");
    vi.mocked(api.resolveApproval)
      .mockRejectedValueOnce(new ApiError(401, "step_up_required"))
      .mockResolvedValueOnce({ ok: true, status: "approved" });
    vi.mocked(api.webauthnLoginOptions).mockResolvedValue({ challenge: "abc" });
    vi.mocked(authenticateWithPasskey).mockResolvedValue({ id: "cred-1" });

    render(<ApprovalsList />);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(useAppStore.getState().pendingApprovals).toHaveLength(0));
    expect(api.resolveApproval).toHaveBeenNthCalledWith(2, 7, true, { id: "cred-1" });
  });

  it("shows an error message when approving fails outright", async () => {
    const { api } = await import("@/core/transport");
    vi.mocked(api.resolveApproval).mockRejectedValue(new ApiError(500, "Server error."));
    render(<ApprovalsList />);

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(await screen.findByText("Server error.")).toBeInTheDocument();
    expect(useAppStore.getState().pendingApprovals).toHaveLength(1);
  });
});

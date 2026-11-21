import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { UNFOLLOW_WARNING } from "@/lib/followFlow";
import { FollowButtonView } from "./FollowButtonView";

const noop = () => {};
const base = { busy: false, error: null, onToggle: noop, onConfirm: noop, onCancel: noop };

describe("FollowButtonView", () => {
  it("shows the toggle only while idle", () => {
    const html = renderToStaticMarkup(<FollowButtonView {...base} following confirming={false} />);
    expect(html).toContain("Paper-following");
    expect(html).not.toContain("Stop following");
  });

  it("asks before unfollowing and states the consequences", () => {
    const html = renderToStaticMarkup(<FollowButtonView {...base} following confirming />);
    expect(html).toContain("Stop following");
    expect(html).toContain("Keep following");
    expect(html).toContain(UNFOLLOW_WARNING);
    expect(html).toContain('role="group"');
  });

  it("offers a plain follow when not following", () => {
    const html = renderToStaticMarkup(<FollowButtonView {...base} following={false} confirming={false} />);
    expect(html).toContain("Paper-follow");
    expect(html).toContain('aria-pressed="false"');
  });

  it("shows a failed request in text", () => {
    const html = renderToStaticMarkup(<FollowButtonView {...base} following confirming={false} error="HTTP 502" />);
    expect(html).toContain("Could not update follow · HTTP 502");
  });
});

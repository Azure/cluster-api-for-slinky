# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

import pulumi

from gitrepo.gitea_builtin import GiteaBuiltinWebhook


def test_webhook_config_matches_flattened_provider_fields() -> None:
    resources = []

    class Mocks(pulumi.runtime.Mocks):
        def new_resource(self, args):
            resources.append(args)
            outputs = dict(args.inputs)
            if args.typ == "gitea:index/repositoryWebhook:RepositoryWebhook":
                outputs["repositoryWebhookId"] = "42"
            return args.name, outputs

        def call(self, args):
            raise AssertionError(f"unexpected invoke: {args.token}")

    pulumi.runtime.set_mocks(Mocks())

    @pulumi.runtime.test
    def check():
        webhook = GiteaBuiltinWebhook(
            "hook",
            api_url="http://gitea.test",
            admin_username="admin",
            admin_password="test-password",
            owner="owner",
            repo_name="gitops",
            webhook_url="http://receiver.test/hook",
            secret="test-token",
        )

        def verify(hook_id):
            assert hook_id == "42"
            hook = next(
                resource for resource in resources
                if resource.typ == "gitea:index/repositoryWebhook:RepositoryWebhook"
            )
            assert hook.inputs["config"] == {
                "url": hook.inputs["url"],
                "content_type": hook.inputs["contentType"],
            }
            assert "secret" not in hook.inputs["config"]
            assert hook.inputs["secret"]["value"] == "test-token"

        return webhook.hook_id.apply(verify)

    check()

import os
import sys
import re
from github import Github, Repository, Label, GithubObject
import yaml


class RepoSetter:
    """
    Changes settings given a config file
    """

    @staticmethod
    def set(repo: Repository, config: dict):
        pass

    @staticmethod
    def name() -> str:
        return "Unnamed reposetter"

    @staticmethod
    def has_changes(new: dict, old) -> bool:
        if len(new) == 0:
            return False

        for k in new:
            if type(old) == dict:
                if k not in old:
                    return True
                old_val = old[k]
            else:
                try:
                    old_val = old.__getattribute__(k)
                except Exception as e:
                    old_val = None

            if old_val != new[k]:
                return True
        return False


class RepoSettings:
    def __init__(self, githubclient: Github):
        self._gh = githubclient
        self._setters = []

    def use(self, setter: RepoSetter):
        self._setters.append(setter)

    def apply(self, config: dict):
        if not self._validate(config):
            raise Exception("Invalid config supplied")

        for name in config['repos']:
            repoconfig = config['repos'][name]
            name = re.sub(r'(https?://)?github\.com/?', '', name)
            repo = self._gh.get_repo(name)

            print(f"Processing repo '{repo.name}'...")
            for setter in self._setters:
                print(f"Using setter '{setter.name()}'")
                setter.set(repo, repoconfig)
            print()

    @staticmethod
    def _validate(config: dict):
        return type(config) == dict \
               and 'repos' in config \
               and type(config['repos']) == dict \
               and len(config['repos']) > 0


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <reposettings.yml>")
        sys.exit(1)

    try:
        config = yaml.safe_load(open(sys.argv[1], 'r'))
    except Exception as e:
        print(f"Could not load settings from {sys.argv[1]}")
        sys.exit(2)

    ghtoken = os.environ.get('GITHUB_TOKEN')
    if ghtoken == "":
        print("Could not read $GITHUB_TOKEN")
        sys.exit(3)

    gh = Github(ghtoken)
    rs = RepoSettings(gh)
    rs.use(RepoHook())
    rs.use(BranchProtectionHook())
    rs.use(LabelHook())

    try:
        rs.apply(config)
    except Exception as e:
        print(str(e))
        exit(10)


class RepoHook(RepoSetter):
    """
    RepoHook handles changing repository settings
    """

    @staticmethod
    def name():
        return "Repo settings hook"

    @staticmethod
    def set(repo: Repository, config):
        print(" Processing repo settings...")
        newsettings = {}

        if 'features' in config:
            for feat in config['features']:
                newsettings[f"has_{feat}"] = config['features'][feat]

        if 'allow' in config:
            for allow in config['allow']:
                newsettings[f"allow_{allow.replace('-', '_')}"] = config['allow'][allow]

        if 'delete-branch-on-merge' in config:
            newsettings['delete_branch_on_merge'] = config['delete-branch-on-merge']

        if not RepoSetter.has_changes(newsettings, repo):
            print(" Repo settings unchanged.")
            return

        print(" Applying new repo settings...")
        repo.edit(**newsettings)


class BranchProtectionHook(RepoSetter):
    """
    BranchProtectionHook handles changing branch protection settings
    """

    @staticmethod
    def name():
        return "Branch protection settings hook"

    @staticmethod
    def set(repo: Repository.Repository, config):
        print(" Processing branch protection settings...")

        if 'branch-protection' not in config and 'branch-protection-overrides' not in config:
            print(" Nothing to do.")
            return

        should_protect_default_branch = config.get('protect-default-branch')

        for branch in repo.get_branches():
            if not (branch.protected or (should_protect_default_branch and repo.default_branch == branch.name)):
                continue

            rules = BranchProtectionHook.rules_for(branch.name, config)

            newsettings = {}
            if 'dissmiss-stale-reviews' in rules:
                newsettings['dismiss_stale_reviews'] = bool(rules['dissmiss-stale-reviews'])
            if 'required-review-count' in rules:
                newsettings['required_approving_review_count'] = int(rules['required-review-count'])

            if not RepoSetter.has_changes(newsettings, branch.get_protection()):
                print(f" Branch protection settings for {branch.name} unchanged.")
                continue

            print(" Applying branch protection settings...")
            branch.edit_protection(**newsettings)

    @staticmethod
    def rules_for(branch_name: str, config):
        if 'branch-protection-overrides' in config:
            overrides = config['branch-protection-overrides']
            if branch_name in overrides:
                return overrides[branch_name]

        if 'branch-protection' in config:
            return config['branch-protection']

        return {}


class LabelHook(RepoSetter):
    """
    LabelHook handles creating and updating labels for repos
    """

    @staticmethod
    def name():
        return "Repo labels settings hook"

    @staticmethod
    def set(repo: Repository, config):
        print(" Processing labels...")

        if 'labels' not in config:
            print(" Nothing to do.")
            return
        conf_labels = config['labels']
        unset_labels = conf_labels.copy()

        repolabels = repo.get_labels()
        for label in repolabels:
            newname, newlabel = LabelHook.replacement(conf_labels, label)
            if newname is None:
                # Not present in config, delete
                print(f" Deleting label {label.name}")
                try:
                    label.delete()
                except Exception as e:
                    print(f" Error deleting label: {str(e)}")
                continue

            if newname != label.name or LabelHook.needs_update(label, newlabel):
                print(f" Editing label {label.name}")
                try:
                    label.edit(
                        name=newname,
                        color=newlabel['color'] if 'color' in newlabel else label.color,
                        description=newlabel['description'] if 'description' in newlabel else label.description,
                    )
                except Exception as e:
                    print(f" Error editing label: {str(e)}")
                    continue

            # Processed, remove from pending
            if newname in unset_labels:
                del unset_labels[newname]

        for newname in unset_labels:
            newlabel = unset_labels[newname]
            print(f" Creating label {newname}")
            try:
                repo.create_label(
                    name=newname,
                    color=newlabel['color'] if 'color' in newlabel and newlabel['color'] is not None
                    else GithubObject.NotSet,
                    description=newlabel['description'] if 'description' in newlabel and newlabel['description'] is not None
                    else GithubObject.NotSet,
                )
            except Exception as e:
                print(f" Error deleting label: {str(e)}")

    @staticmethod
    def needs_update(label: Label, new: dict):
        """
        Checks whether a label needs an update
        """
        if 'color' in new and new['color'] is not None and label.color != new['color']:
            return True
        if 'description' in new and new['description'] is not None and label.description != new['description']:
            return True

        return False

    @staticmethod
    def replacement(newset: dict, label: Label):
        """
        Find in the config a suitable label for replacing the given one, checking keys and `replaces` property
        """
        # Fast path: dict has a key with the old label name
        if label.name in newset:
            return label.name, newset[label.name]

        # Otherwise check `replaces` key for all new labels
        for name, new in newset.items():
            if label.name in new.get('replaces', []):
                return name, new

        return None, None


if __name__ == '__main__':
    main()

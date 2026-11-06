# typed: strict
# frozen_string_literal: true

class Askai < Formula
  desc "Ask AI for shell commands, then choose what to run"
  homepage "https://github.com/ActivationEnergy/askai"
  url "https://github.com/ActivationEnergy/askai.git", using: :git, tag: "v0.1.2"
  version "0.1.2"
  license "MIT"

  depends_on "python@3.14"

  def install
    # No third-party runtime dependencies: keep modules private to this formula.
    libexec.install "src/askai_cli"
    (bin/"askai").write <<~PYTHON
      #!#{formula_opt_bin("python@3.14")}/python3.14
      import sys
      sys.path.insert(0, #{opt_libexec.to_s.inspect})
      from askai_cli.cli import main
      main()
    PYTHON
    (bin/"askai").chmod 0755
    (bin/"askai-config").make_symlink "askai"
    %w[askgpt askds askglm askkimi askmm askdsh askcc].each do |name|
      (bin/name).make_symlink "askai"
    end
    doc.install "README.md"
    (pkgshare/"examples").install "examples/config.toml"
  end

  def caveats
    <<~EOS
      Use an already authenticated Codex CLI, or configure an API provider.
      Run askai-config to configure fish/zsh/bash integration.
      Configure AI models/default backend, or enable direct questions without a prefix.
      Changes are previewed and backed up; reopen the terminal to activate.
      Configuration: ~/.config/askai/config.toml
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/askai --version")
    assert_match "askai-config", shell_output("#{bin}/askai-config --help")
    %w[askgpt askds askglm askkimi askmm askdsh askcc].each do |name|
      assert_match version.to_s, shell_output("#{bin}/#{name} --version")
    end
    assert_match "function askgpt", shell_output("#{bin}/askai init fish --aliases")

    # Exercise the installed prompt, parser and print mode without credentials.
    (testpath/"fake_model.py").write <<~PYTHON
      import json
      import sys
      sys.stdin.read()
      print(json.dumps({
          "explanation": "Homebrew smoke test",
          "commands": [{
              "label": "Print a message",
              "command": "printf homebrew-ok",
              "warning": ""
          }]
      }))
    PYTHON
    config = "provider = \"fixture\"\n"
    %w[fixture codex deepseek glm kimi minimax dsh claude].each do |provider|
      config += <<~TOML
        [providers.#{provider}]
        type = "command"
        executable = "#{formula_opt_bin("python@3.14")}/python3.14"
        args = ["#{testpath}/fake_model.py"]
      TOML
    end
    (testpath/"config.toml").write config
    ENV["ASKAI_CONFIG"] = (testpath/"config.toml").to_s
    ENV.delete "ASKAI_PROVIDER"
    ENV.delete "ASKAI_MODEL"
    ENV.delete "ASKAI_COMMAND_FILE"
    result = shell_output("#{bin}/askai --shell bash --print 'print a greeting'")
    assert_equal "printf homebrew-ok", result.strip
    %w[askgpt askds askglm askkimi askmm askdsh askcc].each do |name|
      result = shell_output("#{bin}/#{name} --shell bash --print 'print a greeting'")
      assert_equal "printf homebrew-ok", result.strip
    end
  end
end

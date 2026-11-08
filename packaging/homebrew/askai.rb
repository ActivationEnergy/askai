# typed: strict
# frozen_string_literal: true

class Askai < Formula
  desc "Ask AI for shell commands, then choose what to run"
  homepage "https://github.com/ActivationEnergy/askai"
  url "https://github.com/ActivationEnergy/askai.git", using: :git, tag: "v0.1.4"
  version "0.1.4"
  license "MIT"

  depends_on "python@3.14"

  resource "prompt-toolkit" do
    url "https://files.pythonhosted.org/packages/84/03/0d3ce49e2505ae70cf43bc5bb3033955d2fc9f932163e84dc0779cc47f48/prompt_toolkit-3.0.52-py3-none-any.whl"
    sha256 "9aac639a3bbd33284347de5ad8d68ecc044b91a762dc39b7c21095fcd6a19955"
  end

  resource "wcwidth" do
    url "https://files.pythonhosted.org/packages/6f/0c/309b1813b230d1dbbd6f2c2ab2bb21ef68992358977605d9ef289bbfedcc/wcwidth-0.9.4-py3-none-any.whl"
    sha256 "901eace6d2dd25ebb917d0ce5db18e5c537e1504c905cf32f0d006c12098b05d"
  end

  def install
    # Pure-Python wheels include package metadata; keep dependencies private.
    resources.each do |r|
      r.fetch unless r.downloaded?
      r.verify_download_integrity(r.cached_download)
      # Wheels are ZIP files, but Homebrew extension detection treats them as plain files.
      system "#{formula_opt_bin("python@3.14")}/python3.14", "-I", "-m", "zipfile", "-e", r.cached_download.to_s, (libexec/"vendor").to_s
    end
    libexec.install "src/askai_cli"
    (bin/"askai").write <<~PYTHON
      #!#{formula_opt_bin("python@3.14")}/python3.14 -I
      import sys
      sys.path.insert(0, #{(opt_libexec/"vendor").to_s.inspect})
      sys.path.insert(0, #{opt_libexec.to_s.inspect})
      from askai_cli.cli import main
      main(launcher=(#{(opt_bin/"askai").to_s.inspect},))
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
    dependency_check = "import sys; sys.path.insert(0, #{(opt_libexec/"vendor").to_s.inspect}); " \
                       "import prompt_toolkit, wcwidth; print(prompt_toolkit.__version__)"
    assert_match "3.0.52", shell_output("#{formula_opt_bin("python@3.14")}/python3.14 -I -c #{dependency_check.shellescape}")
    assert_match version.to_s, shell_output("#{bin}/askai --version")
    assert_match "askai-config", shell_output("#{bin}/askai-config --help")
    %w[askgpt askds askglm askkimi askmm askdsh askcc].each do |name|
      assert_match version.to_s, shell_output("#{bin}/#{name} --version")
    end
    assert_match "function askgpt", shell_output("#{bin}/askai init fish --aliases")
    # Source the installed integration, rather than testing only external aliases.
    [Pathname("/bin/bash"), Pathname("/bin/zsh"), HOMEBREW_PREFIX/"bin/fish"].each do |shell|
      next unless shell.executable?

      init = testpath/"init.#{shell.basename}"
      init.write shell_output("#{bin}/askai init #{shell.basename} --aliases")
      assert_match (opt_bin/"askai").to_s, init.read
      command = "source #{init.to_s.shellescape}; askcc --version; askdsh --version"
      result = shell_output("#{shell} -c #{command.shellescape}")
      assert_equal [version.to_s, version.to_s], result.lines.map(&:strip)
    end

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

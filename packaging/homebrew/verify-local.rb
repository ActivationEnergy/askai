# frozen_string_literal: true

# Run with `brew ruby packaging/homebrew/verify-local.rb` and python@3.14 installed.
# This exercises the real Formula methods, without downloading or linking a keg.
require "formula"
require "tap"
require "formula_assertions"
require "extend/pathname/write_mkpath_extension"
require "tmpdir"
require "fileutils"

Pathname.activate_extensions!
root = Pathname(__dir__).parent.parent
path = root/"packaging/homebrew/askai.rb"
load path
# Supplying the tap identity avoids resolving/installing homebrew/core metadata.
formula = Askai.new("askai", path, :stable, tap: Tap.fetch("activationenergy/tap"))
formula.extend(Homebrew::Assertions)
python = formula.formula_opt_bin("python@3.14")/"python3.14"
raise "Install python@3.14 before running this offline check" unless python.executable?

Dir.mktmpdir("askai-formula-") do |directory|
  temporary = Pathname(directory)
  source = temporary/"source"
  source.mkpath
  # Formula install helpers may move their input: use copies, never the checkout.
  %w[src README.md examples].each { |name| FileUtils.cp_r(root/name, source/name) }
  prefix = temporary/"prefix"
  formula.define_singleton_method(:prefix) { |*_args| prefix }
  formula.define_singleton_method(:opt_prefix) { prefix }
  formula.define_singleton_method(:logs) { temporary/"logs" }
  # Redirect only downloads; production install verifies and extracts the wheels.
  if ARGV.first
    cache = Pathname(ARGV.first).expand_path
    formula.resources.each do |r|
      wheel = cache/File.basename(URI.parse(r.url).path)
      raise "Missing wheel: #{wheel.basename}" unless wheel.file?
      r.downloader.instance_variable_set(:@cached_location, wheel)
    end
  end
  Dir.chdir(source) { formula.install }

  testpath = temporary/"test"
  testpath.mkpath
  formula.instance_variable_set(:@testpath, testpath)
  ENV["HOME"] = (temporary/"home").to_s
  ENV["XDG_CONFIG_HOME"] = (temporary/"config").to_s
  ENV["XDG_STATE_HOME"] = (temporary/"state").to_s
  %w[ASKAI_CONFIG ASKAI_PROVIDER ASKAI_MODEL ASKAI_COMMAND_FILE].each { |name| ENV.delete(name) }
  Dir.chdir(testpath) { formula.test }
  puts "PASS: Homebrew Formula install and test methods in temporary prefix"
end

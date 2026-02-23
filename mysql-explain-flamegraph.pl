#!/usr/bin/perl -w
#
# mysql-explain-flamegraph.pl - Generate flame graph SVG from MySQL EXPLAIN ANALYZE JSON
#
# Thin wrapper: uses unified parser in mysql-explain.pl with --type flamegraph.
# Usage: ./mysql-explain-flamegraph.pl [options] explain.json > query.svg
#
use strict;
use File::Basename;
use File::Spec;

my $script_dir = dirname(File::Spec->rel2abs($0));
my $unified = File::Spec->catfile($script_dir, "mysql-explain.pl");
die "Cannot find mysql-explain.pl in $script_dir\n" unless -x $unified;

exec($unified, "--type", "flamegraph", @ARGV) or die "Cannot exec $unified: $!\n";
